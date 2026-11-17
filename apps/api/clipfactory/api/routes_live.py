"""Live channels: which Twitch channels are watched, what the watcher sees, the moments it detected."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipfactory.api.routes_system import process_alive
from clipfactory.api.serializers import live_channel_out, live_moments_out
from clipfactory.db import get_session
from clipfactory.domain import Profile, live_project_settings
from clipfactory.errors import Conflict, NotFound, ValidationFailed
from clipfactory.live.watcher import HEARTBEAT_KEY
from clipfactory.media import ingest
from clipfactory.models import LiveChannel, LiveMoment

router = APIRouter()
_MOMENTS_SHOWN = 20


@router.get("/live")
def overview(s: Session = Depends(get_session)) -> dict:
    alive = process_alive(s, HEARTBEAT_KEY)
    channels = s.scalars(select(LiveChannel).order_by(LiveChannel.created_at))
    moments = list(s.scalars(select(LiveMoment).order_by(LiveMoment.peak_at.desc()).limit(_MOMENTS_SHOWN)))
    return {"alive": alive, "channels": [live_channel_out(c, alive) for c in channels], "moments": live_moments_out(s, moments)}


class ChannelCreate(BaseModel):
    channel: str = Field(max_length=200)  # a login or a link to the channel page
    rights_confirmed: bool = False
    profile: Profile = "gta6"


@router.post("/live/channels")
def add_channel(body: ChannelCreate, s: Session = Depends(get_session)) -> dict:
    if not body.rights_confirmed:
        raise ValidationFailed("confirm that the streamer authorised you to record and clip this channel")
    login = ingest.parse_twitch_login(body.channel)
    if s.scalar(select(LiveChannel.id).where(LiveChannel.login == login)):
        raise Conflict("this channel is already watched")
    channel = LiveChannel(login=login, rights_confirmed=True, settings=live_project_settings(body.profile).model_dump(mode="json"))
    s.add(channel)
    s.flush()
    return {"channel": live_channel_out(channel, process_alive(s, HEARTBEAT_KEY))}


def _channel(s: Session, channel_id: str) -> LiveChannel:
    channel = s.get(LiveChannel, channel_id)
    if channel is None:
        raise NotFound("channel not found")
    return channel


class ChannelPatch(BaseModel):
    enabled: bool | None = None
    profile: Profile | None = None


@router.patch("/live/channels/{channel_id}")
def patch_channel(channel_id: str, body: ChannelPatch, s: Session = Depends(get_session)) -> dict:
    """Pause or resume a channel, or change the content profile of its future clips. The watcher follows within a second."""
    channel = _channel(s, channel_id)
    if body.enabled is not None:
        channel.enabled = body.enabled
    if body.profile is not None:
        channel.settings = {**channel.settings, "profile": body.profile}
    return {"channel": live_channel_out(channel, process_alive(s, HEARTBEAT_KEY))}


@router.delete("/live/channels/{channel_id}")
def remove_channel(channel_id: str, s: Session = Depends(get_session)) -> dict:
    """Stop watching a channel. Its recorded buffer goes with it; the clips already made stay."""
    s.delete(_channel(s, channel_id))
    return {"deleted": channel_id}
