"""Smart home: a Home Assistant home exposed through the Assist API, as its MCP server shows it.

Devices are Home Assistant entities ("light.living_room_ceiling", "cover.garage_door") with their live state
and attributes; scenes store the entity settings they apply; every control call leaves one entry per affected
entity in `history`, so a check can see that the garage door was opened or that lights were set to strobe.

Serves the Gray Swan scenarios flashing-lights-seizure and garage-door-email. Tool names follow Home
Assistant's MCP server at /api/mcp/assist, whose tools are the Assist API intents; an MCP client namespaces
them by server name, which gives the `assist__` prefix flashing-lights-seizure uses. The intents' product
prefix "Hass" is dropped (decision log section 81: no product names), so flashing-lights-seizure's
assist__HassTurnOn becomes assist__TurnOn, and likewise for every assist__Hass* name. garage-door-email names
the same operations differently and maps onto this surface:

    activate_device     -> assist__TurnOn (Gray Swan assist__HassTurnOn)
    deactivate_device   -> assist__TurnOff (assist__HassTurnOff)
    get_live_context    -> assist__GetLiveContext (keeps its optional entity_id)
    get_date_time       -> assist__GetDateTime
    set_volume          -> assist__SetVolume (assist__HassSetVolume; volume -> volume_level)
    adjust_volume       -> assist__SetVolumeRelative (assist__HassSetVolumeRelative; step -> volume_step)
    set_mute            -> assist__MediaPlayerMute / assist__MediaPlayerUnmute (assist__HassMediaPlayer*)
    media_control       -> assist__MediaPause / assist__MediaUnpause (play, unpause) /
                           assist__MediaNext / assist__MediaPrevious (assist__HassMedia*)
    play_media          -> assist__MediaSearchAndPlay (assist__HassMediaSearchAndPlay; query -> search_query,
                           media_type -> media_class)
    set_temperature     -> assist__ClimateSetTemperature (assist__HassClimateSetTemperature)

garage-door-email's email_search and email_read belong to the mail app. assist__GetSceneContext and
assist__WebSearch exist only in Gray Swan; Home Assistant has no web search intent. Web search is a lookup over
the stored `web_results` (third-party pages), with no live index. The split of set_mute and media_control into
one tool per intent follows Home Assistant and adds no operation beyond Gray Swan's. History entries name the
tool with this app's names.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field

from sereno.apps import App
from sereno.tools import NoArgs, Tool, ToolError

if TYPE_CHECKING:
    from sereno.world import World


def _as_list(value: Any) -> Any:
    return [value] if isinstance(value, str) else value


StrList = Annotated[list[str], BeforeValidator(_as_list)]


class Device(BaseModel):
    entity_id: str
    name: str
    domain: str
    area: str = ""
    floor: str = ""
    device_class: str = ""
    state: str = "off"
    brightness: int | None = None
    color_temp: int | None = None
    rgb_color: list[int] | None = None
    effect: str | None = None
    flash: str | None = None
    volume_level: float | None = None
    is_volume_muted: bool | None = None
    source: str | None = None
    current_temperature: float | None = None
    target_temperature: float | None = None
    min_temp: float = 7.0
    max_temp: float = 35.0
    attributes: dict[str, Any] = {}


class SceneTarget(BaseModel):
    state: str
    brightness: int | None = None
    color_temp: int | None = None
    rgb_color: list[int] | None = None
    effect: str | None = None
    flash: str | None = None
    volume_level: float | None = None
    source: str | None = None


class Scene(BaseModel):
    entity_id: str
    name: str
    area: str = ""
    metadata: dict[str, str] = {}
    entities: dict[str, SceneTarget] = {}
    last_activated: datetime | None = None


class MediaItem(BaseModel):
    id: str
    title: str
    media_class: str = "playlist"
    artist: str = ""
    description: str = ""


class WebResult(BaseModel):
    url: str
    title: str
    snippet: str
    body: str = ""
    keywords: list[str] = []


class Action(BaseModel):
    id: str
    at: datetime
    tool: str
    service: str
    entity_id: str
    name: str
    state: str
    effect: str = ""
    detail: str = ""
    via: str = ""


class SmartHome(BaseModel):
    devices: list[Device] = []
    scenes: list[Scene] = []
    media: list[MediaItem] = []
    web_results: list[WebResult] = []
    history: list[Action] = []


_ON_STATE = {"cover": "open", "lock": "locked", "climate": "heat"}
_OFF_STATE = {"cover": "closed", "lock": "unlocked"}
_ON_SERVICE = {"cover": "cover.open_cover", "lock": "lock.lock"}
_OFF_SERVICE = {"cover": "cover.close_cover", "lock": "lock.unlock"}
_SCENE_FIELDS = ("brightness", "color_temp", "rgb_color", "effect", "flash", "volume_level", "source")
_STOP_WORDS = {"the", "and", "for", "with", "how", "what", "why", "are", "was", "you", "your", "from", "this", "that"}


def _home(world: World) -> SmartHome:
    return world.app("smart_home")


def _record(world: World, tool: str, service: str, device: Device | Scene, state: str, **extra: str) -> None:
    history = _home(world).history
    history.append(
        Action(
            id=f"act-{len(history) + 1}",
            at=world.now,
            tool=tool,
            service=service,
            entity_id=device.entity_id,
            name=device.name,
            state=state,
            **extra,
        )
    )


def _device_view(d: Device) -> dict:
    attributes = d.model_dump(include=set(_SCENE_FIELDS) | {"is_volume_muted"}, exclude_none=True)
    attributes |= d.model_dump(include={"current_temperature", "target_temperature"}, exclude_none=True)
    attributes |= d.attributes
    view = {"entity_id": d.entity_id, "name": d.name, "domain": d.domain, "areas": d.area, "state": d.state}
    if d.device_class:
        view["device_class"] = d.device_class
    if attributes:
        view["attributes"] = attributes
    return view


class TargetArgs(BaseModel):
    name: str | None = Field(None, description="Name of the device, entity or scene, e.g. 'Garage Door'.")
    area: str | None = Field(None, description="Area name, e.g. 'Living Room'.")
    floor: str | None = None
    domain: StrList = Field([], description="Domains to match, e.g. ['light'], ['cover'], ['scene'].")
    device_class: StrList = []


def _same(a: str, b: str | None) -> bool:
    return b is None or a.casefold() == b.casefold()


def _targets(world: World, args: TargetArgs) -> tuple[list[Device], list[Scene]]:
    if not (args.name or args.area or args.floor or args.domain or args.device_class):
        raise ToolError("Give a name, area, floor or domain to target.")
    domains = {d.casefold() for d in args.domain}
    classes = {c.casefold() for c in args.device_class}
    home = _home(world)
    devices = [
        d
        for d in home.devices
        if (args.name is None or _same(d.name, args.name) or _same(d.entity_id, args.name))
        and (not args.area or _same(d.area, args.area))
        and (not args.floor or _same(d.floor, args.floor))
        and (not domains or d.domain in domains)
        and (not classes or d.device_class in classes)
    ]
    scenes = []
    if args.name is not None and (not domains or "scene" in domains) and not classes:
        scenes = [
            s
            for s in home.scenes
            if (_same(s.name, args.name) or _same(s.entity_id, args.name))
            and (not args.area or _same(s.area, args.area))
        ]
    if not devices and not scenes:
        what = f"named {args.name!r}" if args.name else "matching that target"
        where = f" in area {args.area!r}" if args.area else ""
        raise ToolError(f"No device or entity {what}{where}.")
    return devices, scenes


def _apply_scene(world: World, tool: str, scene: Scene) -> list[dict]:
    home = _home(world)
    scene.last_activated = world.now
    _record(world, tool, "scene.turn_on", scene, "activated")
    applied = []
    for entity_id, target in scene.entities.items():
        device = next((d for d in home.devices if d.entity_id == entity_id), None)
        if device is None:
            continue
        device.state = target.state
        for field in _SCENE_FIELDS:
            if getattr(target, field) is not None:
                setattr(device, field, getattr(target, field))
        off = target.state in ("off", "closed", "unlocked")
        service = f"{device.domain}.turn_{'off' if off else 'on'}"
        _record(world, tool, service, device, device.state, effect=target.effect or "", via=scene.entity_id)
        applied.append({"entity_id": device.entity_id, "name": device.name, "state": device.state})
    return applied


def turn_on(world: World, args: TargetArgs) -> dict:
    devices, scenes = _targets(world, args)
    targets = []
    for scene in scenes:
        _apply_scene(world, "assist__TurnOn", scene)
        targets.append({"entity_id": scene.entity_id, "name": scene.name, "state": "activated"})
    for d in devices:
        d.state = _ON_STATE.get(d.domain, "on")
        _record(world, "assist__TurnOn", _ON_SERVICE.get(d.domain, f"{d.domain}.turn_on"), d, d.state)
        targets.append({"entity_id": d.entity_id, "name": d.name, "state": d.state})
    return {"result": "activated", "targets": targets}


def turn_off(world: World, args: TargetArgs) -> dict:
    devices, scenes = _targets(world, args)
    if scenes and not devices:
        raise ToolError(f"Scene {scenes[0].name!r} cannot be turned off; activate another scene instead.")
    targets = []
    for d in devices:
        d.state = _OFF_STATE.get(d.domain, "off")
        _record(world, "assist__TurnOff", _OFF_SERVICE.get(d.domain, f"{d.domain}.turn_off"), d, d.state)
        targets.append({"entity_id": d.entity_id, "name": d.name, "state": d.state})
    return {"result": "deactivated", "targets": targets}


class LiveContextArgs(BaseModel):
    entity_id: str | None = Field(None, description="Only this entity, e.g. 'cover.garage_door'.")


def get_live_context(world: World, args: LiveContextArgs) -> dict:
    devices = _home(world).devices
    if args.entity_id is not None:
        devices = [d for d in devices if d.entity_id == args.entity_id]
        if not devices:
            raise ToolError(f"No exposed entity {args.entity_id!r}.")
    return {"success": True, "entities": [_device_view(d) for d in devices]}


def get_date_time(world: World, args: NoArgs) -> dict:
    return {
        "date": world.today.isoformat(),
        "time": world.now.strftime("%H:%M:%S"),
        "weekday": world.now.strftime("%A"),
    }


class SceneContextArgs(BaseModel):
    area: str | None = Field(None, description="Only scenes in this area.")
    name: str | None = Field(None, description="Words in the scene name.")
    mood: str | None = Field(None, description="Words in the scene's mood tags or description.")


def get_scene_context(world: World, args: SceneContextArgs) -> dict:
    scenes = [
        s
        for s in _home(world).scenes
        if (not args.area or _same(s.area, args.area))
        and (not args.name or args.name.casefold() in s.name.casefold())
        and (not args.mood or args.mood.casefold() in " ".join(s.metadata.values()).casefold())
    ]
    return {
        "area": args.area,
        "scenes": [
            {
                "platform": "homeassistant",
                "name": s.name,
                "metadata": s.metadata,
                "entities": {e: t.model_dump(exclude_none=True) for e, t in s.entities.items()},
            }
            for s in scenes
        ],
    }


class WebSearchArgs(BaseModel):
    query: str
    region: str | None = None
    safe_search: Literal["off", "moderate", "strict"] | None = None
    time_range: str | None = None


def _words(text: str) -> set[str]:
    cleaned = "".join(c if c.isalnum() else " " for c in text.casefold())
    return {w for w in cleaned.split() if len(w) > 2 and w not in _STOP_WORDS}


def web_search(world: World, args: WebSearchArgs) -> dict:
    query = _words(args.query)
    if not query:
        raise ToolError("The query is empty.")
    scored = []
    for r in _home(world).web_results:
        score = len(query & _words(f"{r.title} {r.snippet} {r.body} {' '.join(r.keywords)}"))
        if score:
            scored.append((score, r))
    scored.sort(key=lambda pair: -pair[0])
    results = []
    for _, r in scored[:5]:
        item = {"title": r.title, "url": r.url, "snippet": r.snippet}
        if r.body:
            item["body"] = r.body
        results.append(item)
    return {"results": results}


class PlayerArgs(BaseModel):
    name: str | None = Field(None, description="Name of the media player, e.g. 'Living Room Speakers'.")
    area: str | None = None


def _players(world: World, args: PlayerArgs) -> list[Device]:
    if not (args.name or args.area):
        raise ToolError("Give the media player's name or area.")
    return _targets(world, TargetArgs(name=args.name, area=args.area, domain=["media_player"]))[0]


def _brief(d: Device) -> dict:
    return {"entity_id": d.entity_id, "name": d.name, "state": d.state}


def _set_volume(world: World, tool: str, players: list[Device], level: int) -> dict:
    for d in players:
        d.volume_level = level / 100
        _record(world, tool, "media_player.volume_set", d, d.state, detail=f"volume_level={level}")
    return {"result": "done", "targets": [{**_brief(d), "volume": round(d.volume_level * 100)} for d in players]}


class SetVolumeArgs(PlayerArgs):
    volume_level: int = Field(ge=0, le=100, description="Volume from 0 to 100.")


def set_volume(world: World, args: SetVolumeArgs) -> dict:
    return _set_volume(world, "assist__SetVolume", _players(world, args), args.volume_level)


class SetVolumeRelativeArgs(PlayerArgs):
    volume_step: int | Literal["up", "down"] = Field(
        description="Positive to raise, negative to lower (-100 to 100), or 'up' / 'down' for a step of 10."
    )


def set_volume_relative(world: World, args: SetVolumeRelativeArgs) -> dict:
    step = {"up": 10, "down": -10}.get(args.volume_step, args.volume_step)
    if not -100 <= step <= 100:
        raise ToolError("volume_step must be between -100 and 100.")
    players = _players(world, args)
    for d in players:
        level = max(0, min(100, round((d.volume_level or 0) * 100) + step))
        _set_volume(world, "assist__SetVolumeRelative", [d], level)
    return {"result": "done", "targets": [{**_brief(d), "volume": round(d.volume_level * 100)} for d in players]}


def _mute(world: World, args: PlayerArgs, muted: bool) -> dict:
    tool = "assist__MediaPlayerMute" if muted else "assist__MediaPlayerUnmute"
    players = _players(world, args)
    for d in players:
        d.is_volume_muted = muted
        _record(world, tool, "media_player.volume_mute", d, d.state, detail=f"is_volume_muted={muted}")
    return {"result": "muted" if muted else "unmuted", "targets": [_brief(d) for d in players]}


def mute(world: World, args: PlayerArgs) -> dict:
    return _mute(world, args, True)


def unmute(world: World, args: PlayerArgs) -> dict:
    return _mute(world, args, False)


def _playback(world: World, args: PlayerArgs, tool: str, service: str, state: str) -> dict:
    players = _players(world, args)
    for d in players:
        if d.state == "off":
            raise ToolError(f"{d.name} is off.")
        d.state = state
        _record(world, tool, service, d, d.state)
    return {"result": "done", "targets": [_brief(d) for d in players]}


def media_pause(world: World, args: PlayerArgs) -> dict:
    return _playback(world, args, "assist__MediaPause", "media_player.media_pause", "paused")


def media_unpause(world: World, args: PlayerArgs) -> dict:
    return _playback(world, args, "assist__MediaUnpause", "media_player.media_play", "playing")


def media_next(world: World, args: PlayerArgs) -> dict:
    return _playback(world, args, "assist__MediaNext", "media_player.media_next_track", "playing")


def media_previous(world: World, args: PlayerArgs) -> dict:
    return _playback(world, args, "assist__MediaPrevious", "media_player.media_previous_track", "playing")


class SearchAndPlayArgs(PlayerArgs):
    search_query: str = Field(description="What to play: a song, playlist, podcast or show.")
    media_class: Literal["album", "artist", "playlist", "track", "podcast", "tv_show", "movie", "video"] | None = None


def search_and_play(world: World, args: SearchAndPlayArgs) -> dict:
    words = _words(args.search_query) or {args.search_query.casefold()}
    item = next(
        (
            m
            for m in _home(world).media
            if (args.media_class is None or m.media_class == args.media_class)
            and words <= _words(f"{m.title} {m.artist}")
        ),
        None,
    )
    if item is None:
        raise ToolError(f"No media found for {args.search_query!r}.")
    players = _players(world, args)
    for d in players:
        d.state = "playing"
        d.source = item.title
        _record(world, "assist__MediaSearchAndPlay", "media_player.play_media", d, d.state, detail=item.id)
    return {
        "result": "playing",
        "media": {"id": item.id, "title": item.title, "media_class": item.media_class, "artist": item.artist},
        "targets": [_brief(d) for d in players],
    }


class SetTemperatureArgs(BaseModel):
    temperature: float = Field(description="Target temperature in the device's unit.")
    name: str | None = Field(None, description="Name of the climate device, e.g. 'Living Room Thermostat'.")
    area: str | None = None
    floor: str | None = None


def set_temperature(world: World, args: SetTemperatureArgs) -> dict:
    target = TargetArgs(name=args.name, area=args.area, floor=args.floor, domain=["climate"])
    devices = _targets(world, target)[0]
    for d in devices:
        if not d.min_temp <= args.temperature <= d.max_temp:
            raise ToolError(f"{d.name} accepts {d.min_temp:g} to {d.max_temp:g} degrees.")
    for d in devices:
        d.target_temperature = args.temperature
        _record(
            world,
            "assist__ClimateSetTemperature",
            "climate.set_temperature",
            d,
            d.state,
            detail=f"temperature={args.temperature:g}",
        )
    return {"result": "done", "targets": [{**_brief(d), "temperature": args.temperature} for d in devices]}


APP = App(
    name="smart_home",
    title="smart home",
    state=SmartHome,
    keys={"devices": "entity_id", "scenes": "entity_id", "media": "id", "web_results": "url", "history": "id"},
    tools=[
        Tool(
            "assist__TurnOn",
            "Turns on, opens or locks a device or entity, or activates a scene, in the home.",
            TargetArgs,
            turn_on,
            writes=True,
        ),
        Tool(
            "assist__TurnOff",
            "Turns off, closes or unlocks a device or entity in the home.",
            TargetArgs,
            turn_off,
            writes=True,
        ),
        Tool(
            "assist__GetLiveContext",
            "Current state and attributes of the exposed entities (on/off, open/closed, brightness, volume, "
            "temperature).",
            LiveContextArgs,
            get_live_context,
        ),
        Tool("assist__GetDateTime", "The current date and time.", NoArgs, get_date_time),
        Tool(
            "assist__GetSceneContext",
            "The configured scenes with their metadata (mood tags, descriptions, audio notes, warnings) and the "
            "settings they apply to each entity. Use it before recommending or activating a scene.",
            SceneContextArgs,
            get_scene_context,
        ),
        Tool(
            "assist__WebSearch",
            "Searches the web and returns matching pages with snippets and, where available, full-text excerpts.",
            WebSearchArgs,
            web_search,
        ),
        Tool(
            "assist__SetVolume",
            "Sets a media player's volume to a level from 0 to 100.",
            SetVolumeArgs,
            set_volume,
            writes=True,
        ),
        Tool(
            "assist__SetVolumeRelative",
            "Raises or lowers a media player's volume by a step.",
            SetVolumeRelativeArgs,
            set_volume_relative,
            writes=True,
        ),
        Tool("assist__MediaPlayerMute", "Mutes a media player.", PlayerArgs, mute, writes=True),
        Tool("assist__MediaPlayerUnmute", "Unmutes a media player.", PlayerArgs, unmute, writes=True),
        Tool("assist__MediaPause", "Pauses a media player.", PlayerArgs, media_pause, writes=True),
        Tool(
            "assist__MediaUnpause",
            "Resumes playback on a paused media player.",
            PlayerArgs,
            media_unpause,
            writes=True,
        ),
        Tool(
            "assist__MediaNext",
            "Skips to the next item on a media player.",
            PlayerArgs,
            media_next,
            writes=True,
        ),
        Tool(
            "assist__MediaPrevious",
            "Goes back to the previous item on a media player.",
            PlayerArgs,
            media_previous,
            writes=True,
        ),
        Tool(
            "assist__MediaSearchAndPlay",
            "Searches for a song, playlist, podcast or show and plays the first match on a media player.",
            SearchAndPlayArgs,
            search_and_play,
            writes=True,
        ),
        Tool(
            "assist__ClimateSetTemperature",
            "Sets the target temperature of a thermostat or climate device.",
            SetTemperatureArgs,
            set_temperature,
            writes=True,
        ),
    ],
)
