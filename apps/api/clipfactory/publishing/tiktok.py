"""TikTok publishing adapter — intentionally disabled.

CLIPFACTORY is export-first: every clip is downloadable with its caption,
hashtags and subtitle sidecar. Direct publishing needs a TikTok developer app
that the user does not have yet, so nothing here talks to TikTok, and nothing
pretends to. No unofficial automation is, or will be, used.

What a real implementation must do (official Content Posting API only):

1. Credentials: ``TIKTOK_CLIENT_KEY`` / ``TIKTOK_CLIENT_SECRET`` in ``.env``, read server-side only.
2. OAuth 2.0 authorization-code flow with PKCE; scope ``video.publish`` (direct post) or
   ``video.upload`` (send to the creator's inbox as a draft). Tokens stored encrypted, refreshed on expiry.
3. Before every post, query the creator-info endpoint and honour what it returns: allowed privacy
   levels, max duration, and whether comments / duet / stitch may be enabled.
4. Unaudited apps can only post with private visibility — surface that restriction instead of hiding it.
5. Require an explicit per-clip confirmation from the user in the dashboard; never auto-publish.
6. Upload, then poll the publish-status endpoint and record every state in ``publishing_records``.
   A post is "published" only when TikTok says so.

Verify each of these against TikTok's current documentation before writing the client.
"""

from __future__ import annotations

from clipfactory.errors import ClipFactoryError


class PublishingDisabled(ClipFactoryError):
    code = "publishing_disabled"
    http_status = 409


def tiktok_status() -> dict:
    return {
        "platform": "tiktok",
        "enabled": False,
        "configured": False,
        "reason": "Aucune application TikTok Developer configurée. Les clips s'exportent en MP4 avec légende, hashtags et sous-titres.",
        "requirements": [
            "Application TikTok Developer avec le produit Content Posting API",
            "TIKTOK_CLIENT_KEY et TIKTOK_CLIENT_SECRET dans .env",
            "Autorisation OAuth du compte (scope video.publish ou video.upload)",
            "Confirmation explicite avant chaque publication",
        ],
    }


def publish_clip(clip_id: str) -> None:
    """Always refuses. Kept so callers have one place to integrate against later."""
    raise PublishingDisabled("TikTok publishing is not configured; export the clip instead", clip_id=clip_id)
