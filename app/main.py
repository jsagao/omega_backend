# main.py
import os
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from datetime import datetime
import re, html
from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel, Field, Session, create_engine, select
from sqlalchemy import or_, func
from rss_finance_home import router as rss_router
import yfinance as yf

load_dotenv() 
# ---------- Helpers ----------
def html_to_text(s: str) -> str:
    """Very simple HTML → text (good enough for building an excerpt)."""
    text = re.sub(r"<[^>]*>", "", s or "")
    return html.unescape(text).strip()


# ---------- Models ----------
class PostBase(SQLModel):
    title: str
    category: str = "general"
    author: str = "anonymous"
    excerpt: str = ""      # short, plain text preview
    content: str = ""      # full HTML (from ReactQuill)
    description: str = ""  # deprecated (kept for back-compat)
    # featured controls
    featured_slot: str = "none"         # "none" | "main" | "mini"
    featured_rank: Optional[int] = None # smaller number = higher priority
    cover_image_url: str = ""           # image for posts

class Post(PostBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

class PostCreate(PostBase):
    pass

class PostUpdate(SQLModel):
    title: Optional[str] = None
    category: Optional[str] = None
    author: Optional[str] = None
    excerpt: Optional[str] = None
    content: Optional[str] = None
    description: Optional[str] = None  # deprecated
    featured_slot: Optional[str] = None
    featured_rank: Optional[int] = None
    cover_image_url: Optional[str] = None

# ---------- Models (existing Comment stuff) ----------
class Comment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    post_id: int = Field(index=True)
    author: str = "anonymous"
    body: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)

class CommentCreate(SQLModel):
    post_id: int
    author: str
    body: str


# Read DATABASE_URL from environment, default to sqlite for dev
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./blog.db")

# Normalize old-style URIs (e.g., "postgres://...") to the psycopg3 URL
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)

# Supabase/Postgres needs SSL; SQLite does not
connect_args = {}
if DATABASE_URL.startswith("postgresql+psycopg://") or DATABASE_URL.startswith("postgresql://"):
    connect_args = {"sslmode": "require"}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,   # helps avoid stale connections
    echo=False,
)

def init_db():
    # create tables if they don't exist
    SQLModel.metadata.create_all(engine)

from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


# ---------- App ----------
app = FastAPI(title="Blog API", lifespan=lifespan)

# CORS (Vite dev)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://<your-vercel-domain>.vercel.app",  # add your vercel domain later
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include the RSS router (your existing module)
app.include_router(rss_router)


# ---------- Routes ----------
@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/posts", response_model=List[Post])
def list_posts(
    q: Optional[str] = None,
    cat: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> List[Post]:
    """List posts with optional case-insensitive search & category filter."""
    limit = max(1, min(limit or 20, 100))
    offset = max(0, offset or 0)

    with Session(engine) as session:
        stmt = select(Post)

        # text search (case-insensitive) across title/excerpt/content
        if q:
            q_norm = q.strip().lower()
            if q_norm:
                like = f"%{q_norm}%"
                stmt = stmt.where(
                    or_(
                        func.lower(Post.title).like(like),
                        func.lower(Post.excerpt).like(like),
                        func.lower(Post.content).like(like),
                    )
                )

        # category filter (case-insensitive)
        if cat:
            stmt = stmt.where(func.lower(Post.category) == cat.strip().lower())

        # newest first
        stmt = stmt.order_by(Post.created_at.desc(), Post.id.desc())

        # pagination
        stmt = stmt.offset(offset).limit(limit)

        return session.exec(stmt).all()


@app.get("/posts/{post_id}", response_model=Post)
def get_post(post_id: int) -> Post:
    with Session(engine) as session:
        post = session.get(Post, post_id)
        if not post:
            raise HTTPException(404, "Post not found")
        return post


@app.post("/posts", response_model=Post, status_code=201)
def create_post(payload: PostCreate) -> Post:
    with Session(engine) as session:
        data = payload.dict()

        # auto-fill excerpt if missing but content present
        if not data.get("excerpt") and data.get("content"):
            data["excerpt"] = html_to_text(data["content"])[:220]

        # keep legacy description in sync (optional)
        if not data.get("description"):
            data["description"] = data.get("excerpt", "")

        post = Post(**data)
        session.add(post)
        session.commit()
        session.refresh(post)
        return post


# ---------- Robust Quotes (fixes yfinance KeyError) ----------
def safe_price(tkr: yf.Ticker):
    """Return (last, prev_close, exchange, currency) safely across stocks/indices/FX/futures."""
    last = prev = exch = curr = None

    # 1) Try fast_info when available
    fi = getattr(tkr, "fast_info", None)
    if fi:
        try:
            v = getattr(fi, "last_price", None)
            if v is not None:
                last = float(v)
        except Exception:
            pass
        try:
            v = getattr(fi, "previous_close", None)
            if v is not None:
                prev = float(v)
        except Exception:
            pass
        try:
            exch = getattr(fi, "exchange", None)
        except Exception:
            pass
        try:
            curr = getattr(fi, "currency", None)
        except Exception:
            pass

    # 2) Fallback to .info (can be slow / sometimes limited)
    if last is None or prev is None or curr is None or exch is None:
        try:
            inf = tkr.info or {}
            if last is None and inf.get("regularMarketPrice") is not None:
                last = float(inf["regularMarketPrice"])
            if prev is None and inf.get("regularMarketPreviousClose") is not None:
                prev = float(inf["regularMarketPreviousClose"])
            if curr is None:
                curr = inf.get("currency")
            if exch is None:
                exch = inf.get("exchange") or inf.get("fullExchangeName")
        except Exception:
            pass

    # 3) Fallback to recent history (most reliable)
    if last is None or prev is None:
        try:
            hist = tkr.history(period="5d", interval="1d", auto_adjust=False, prepost=True)
            if not hist.empty:
                closes = hist["Close"].dropna()
                if not closes.empty:
                    if last is None:
                        last = float(closes.iloc[-1])
                    if prev is None and len(closes) >= 2:
                        prev = float(closes.iloc[-2])
        except Exception:
            pass

    return last, prev, exch, curr


@app.get("/api/quotes")
def get_quotes(symbols: str = Query(..., description="Comma-separated symbols e.g. AAPL,MSFT,^GSPC,BTC-USD,CL=F")):
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    out: List[Dict[str, Any]] = []
    for s in syms:
        try:
            # Use single Ticker to reduce edge cases compared to Tickers aggregator
            t = yf.Ticker(s)
            last, prev, exch, curr = safe_price(t)

            chg = pct = None
            if last is not None and prev is not None:
                chg = last - prev
                pct = (chg / prev * 100.0) if prev else None

            out.append({
                "symbol": s,
                "price": last,
                "prevClose": prev,
                "change": chg,
                "percent": pct,
                "exchange": exch,
                "currency": curr,
            })
        except Exception:
            # Return a safe row instead of throwing 500s
            out.append({
                "symbol": s,
                "price": None,
                "prevClose": None,
                "change": None,
                "percent": None,
                "exchange": None,
                "currency": None,
            })
    return {"quotes": out}


@app.patch("/posts/{post_id}", response_model=Post)
def update_post(post_id: int, payload: PostUpdate) -> Post:
    with Session(engine) as session:
        post = session.get(Post, post_id)
        if not post:
            raise HTTPException(404, "Post not found")

        data = payload.dict(exclude_unset=True)

        # if content changed and excerpt not provided, regenerate excerpt
        if "content" in data and "excerpt" not in data:
            data["excerpt"] = html_to_text(data["content"])[:220]

        # keep legacy description in sync if excerpt updated
        if "excerpt" in data and "description" not in data:
            data["description"] = data["excerpt"]

        for k, v in data.items():
            setattr(post, k, v)

        session.add(post)
        session.commit()
        session.refresh(post)
        return post


@app.delete("/posts/{post_id}", status_code=204)
def delete_post(post_id: int) -> None:
    """
    Idempotent delete:
    - If the post exists -> delete it, return 204
    - If it doesn't exist -> still return 204 (no error)
    """
    with Session(engine) as session:
        post = session.get(Post, post_id)
        if post:
            session.delete(post)
            session.commit()
    # No response body for 204


@app.get("/featured")
def get_featured(limit_minis: int = Query(5, ge=1, le=8)) -> Dict[str, Any]:
    with Session(engine) as session:
        # MAIN (manual first)
        main_stmt = (
            select(Post)
            .where(Post.featured_slot == "main")
            .order_by(
                func.coalesce(Post.featured_rank, 999999).asc(),
                Post.created_at.desc(),
                Post.id.desc(),
            )
            .limit(1)
        )
        main = session.exec(main_stmt).first()

        if not main:
            main = session.exec(
                select(Post).order_by(Post.created_at.desc(), Post.id.desc()).limit(1)
            ).first()

        used_ids = {main.id} if main else set()

        # MINIS (manual first)
        minis_stmt = (
            select(Post)
            .where(Post.featured_slot == "mini", Post.id.not_in(used_ids))
            .order_by(
                func.coalesce(Post.featured_rank, 999999).asc(),
                Post.created_at.desc(),
                Post.id.desc(),
            )
            .limit(limit_minis)
        )
        minis = session.exec(minis_stmt).all()

        if len(minis) < limit_minis:
            fill_stmt = (
                select(Post)
                .where(Post.id.not_in(used_ids.union({p.id for p in minis})))
                .order_by(Post.created_at.desc(), Post.id.desc())
                .limit(limit_minis - len(minis))
            )
            minis += session.exec(fill_stmt).all()

        return {"main": main, "minis": minis}


@app.get("/posts/{post_id}/comments", response_model=List[Comment])
def list_comments(post_id: int, limit: int = 50, offset: int = 0) -> List[Comment]:
    limit = max(1, min(limit or 50, 200))
    offset = max(0, offset or 0)
    with Session(engine) as session:
        stmt = (
            select(Comment)
            .where(Comment.post_id == post_id)
            .order_by(Comment.created_at.desc(), Comment.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return session.exec(stmt).all()
    
@app.get("/portfolio", response_model=List[Post])
def list_portfolio(limit: int = 20, offset: int = 0) -> List[Post]:
    limit = max(1, min(limit or 20, 50))
    offset = max(0, offset or 0)
    with Session(engine) as session:
        stmt = (
            select(Post)
            .where(Post.featured_slot == "portfolio")
            .order_by(
                func.coalesce(Post.featured_rank, 999999).asc(),
                Post.created_at.desc(),
                Post.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        return session.exec(stmt).all()


@app.post("/posts/{post_id}/comments", response_model=Comment, status_code=201)
def create_comment(post_id: int, payload: CommentCreate) -> Comment:
    if payload.post_id != post_id:
        raise HTTPException(400, "post_id mismatch")
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(400, "Comment body is required")
    with Session(engine) as session:
        # optional: ensure post exists
        if not session.get(Post, post_id):
            raise HTTPException(404, "Post not found")

        c = Comment(post_id=post_id, author=(payload.author or "anonymous").strip(), body=body)
        session.add(c)
        session.commit()
        session.refresh(c)
        return c