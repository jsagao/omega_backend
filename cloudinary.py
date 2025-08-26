# app/routers/cloudinary.py
from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import cloudinary
import cloudinary.api

router = APIRouter()


class DeleteByTokensBody(BaseModel):
    delete_tokens: List[str] = Field(default_factory=list)


class DeleteByPublicIdsBody(BaseModel):
    public_ids: List[str] = Field(default_factory=list, description="folder/name without extension")
    resource_type: str = Field(default="image", description="image|video|raw")
    type: str = Field(default="upload", description="typically 'upload'")


@router.post("/delete-by-tokens")
def delete_by_tokens(body: DeleteByTokensBody):
    tokens = [t for t in body.delete_tokens if t]
    if not tokens:
        return {"ok": True, "deleted": [], "failed": []}

    deleted, failed = [], []
    for tok in tokens:
        try:
            res = cloudinary.api.delete_resources_by_token(tok)
            deleted.append({"token": tok, "result": res})
        except Exception as e:
            failed.append({"token": tok, "error": str(e)})

    return {"ok": True, "deleted": deleted, "failed": failed}


@router.post("/delete-by-public-ids")
def delete_by_public_ids(body: DeleteByPublicIdsBody):
    pids = [pid for pid in body.public_ids if pid]
    if not pids:
        return {"ok": True, "result": {"deleted": {}}}

    try:
        res = cloudinary.api.delete_resources(
            public_ids=pids,
            type=body.type,
            resource_type=body.resource_type,
            invalidate=True,  # purge CDN
        )
        return {"ok": True, "result": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cloudinary delete failed: {e}")