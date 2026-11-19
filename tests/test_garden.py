from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from cogs.garden import describe, summarize
from cogs.panel_bridge import PanelBridge
from utils.garden import render, rules, store

T0 = 1_800_000_000.0
RULES = rules.load()
PER_WATER = RULES["barrel"]["seconds_per_water"]
needs_art = pytest.mark.skipif(not render.available(), reason="the bought sprite packs are not in git")


def ledger_total(storage, user_id: int, currency: str) -> int:
    with store.connect(storage) as connection:
        return connection.execute("SELECT COALESCE(SUM(amount), 0) FROM garden_ledger WHERE user_id = ? AND currency = ?", (user_id, currency)).fetchone()[0]


def test_a_new_greenhouse_has_a_full_barrel_starting_resources_and_an_empty_pot(storage):
    state = store.get_state(storage, 1, now=T0)
    assert state["new"] and state["water"] == RULES["starting"]["water"] and state["sunlight"] == RULES["starting"]["sunlight"]
    assert state["barrel"]["stored"] == state["barrel"]["capacity"] and state["barrel"]["full"] and state["barrel"]["next_at"] is None
    assert [pot["locked"] for pot in state["pots"]] == [False, True, True] and state["plant"] is None
    assert [s["species"] for s in state["starters"]] == RULES["starters"]
    assert state["can"] == {"collect": True, "plant": True, "water": False, "bench": False}
    assert not store.get_state(storage, 1, now=T0)["new"] and store.is_player(storage, 1) and not store.is_player(storage, 2)


def test_the_barrel_fills_over_time_and_keeps_the_part_filled_unit(storage):
    store.get_state(storage, 1, now=T0)
    state, did = store.collect_water(storage, 1, now=T0)
    assert did == "collected 8 water" and state["water"] == RULES["starting"]["water"] + 8 and state["barrel"]["stored"] == 0
    assert state["barrel"]["next_at"] == T0 + PER_WATER
    with pytest.raises(store.GardenError, match="empty"):
        store.collect_water(storage, 1, now=T0 + PER_WATER - 1)
    # two and a half units later: two can be collected, and the half is not lost
    state, _ = store.collect_water(storage, 1, now=T0 + PER_WATER * 2.5)
    assert state["water"] == RULES["starting"]["water"] + 10 and state["barrel"]["next_at"] == T0 + PER_WATER * 3
    assert store.get_state(storage, 1, now=T0 + PER_WATER * 500)["barrel"]["stored"] == 8  # never above its capacity
    assert ledger_total(storage, 1, "water") == RULES["starting"]["water"] + 10


def test_a_plant_grows_one_stage_per_watering_until_it_blooms_then_moves_to_the_bench(storage):
    store.get_state(storage, 1, now=T0)
    store.collect_water(storage, 1, now=T0)
    with pytest.raises(store.GardenError, match="Plant a seed"):
        store.water_plant(storage, 1, now=T0)
    with pytest.raises(store.GardenError, match="not available"):
        store.plant_seed(storage, 1, "daisy", now=T0)  # a real species, but not a starter
    state, did = store.plant_seed(storage, 1, "tulip", now=T0)
    assert did == "planted a Tulip seed" and state["plant"]["stage"] == 0 and state["plant"]["needs_water"] and state["starters"] == []
    with pytest.raises(store.GardenError, match="already has a plant"):
        store.plant_seed(storage, 1, "tulip", now=T0)
    with store.connect(storage) as connection:  # enough for the whole life of the plant
        connection.execute("UPDATE garden_players SET sunlight = 20, water = 20 WHERE user_id = 1")

    now = T0
    for stage, grow in enumerate(RULES["grow_seconds"]):
        state, did = store.water_plant(storage, 1, now=now)
        assert did == "watered their Tulip" and state["plant"]["growing"] and state["plant"]["ready_at"] == now + grow
        with pytest.raises(store.GardenError, match="already growing"):
            store.water_plant(storage, 1, now=now + grow - 1)
        now += grow
        assert store.get_state(storage, 1, now=now)["plant"]["stage"] == stage + 1
    state = store.get_state(storage, 1, now=now)
    assert state["plant"]["mature"] and state["plant"]["stage_name"] == "In bloom" and state["can"]["bench"] and not state["can"]["water"]
    with pytest.raises(store.GardenError, match="fully grown"):
        store.water_plant(storage, 1, now=now)
    state, did = store.move_to_bench(storage, 1, now=now)
    assert did == "moved a grown Tulip to the bench" and state["plant"] is None and state["bench"] == 1 and state["can"]["plant"]
    with pytest.raises(store.GardenError, match="fully grown"):
        store.move_to_bench(storage, 1, now=now)


def test_watering_costs_water_and_sunlight_and_says_which_is_missing(storage):
    store.get_state(storage, 1, now=T0)
    store.plant_seed(storage, 1, "sunflower", now=T0)
    cost = RULES["watering"]
    state, _ = store.water_plant(storage, 1, now=T0)
    assert state["water"] == RULES["starting"]["water"] - cost["water"] and state["sunlight"] == RULES["starting"]["sunlight"] - cost["sunlight"]
    with store.connect(storage) as connection:
        connection.execute("UPDATE garden_players SET water = 0 WHERE user_id = 1")
    later = T0 + RULES["grow_seconds"][0]
    with pytest.raises(store.GardenError, match="rain barrel"):
        store.water_plant(storage, 1, now=later)
    with store.connect(storage) as connection:
        connection.execute("UPDATE garden_players SET water = 9, sunlight = 0 WHERE user_id = 1")
    with pytest.raises(store.GardenError, match="chatting"):
        store.water_plant(storage, 1, now=later)
    assert store.get_state(storage, 1, now=later)["plant"]["stage"] == 1  # a refused watering changes nothing
    with pytest.raises(store.GardenError, match="Unknown"):
        store.act(storage, 1, "explode")


def test_sunlight_from_chatting_is_only_for_players_and_stops_at_the_daily_cap(storage):
    assert store.add_sunlight(storage, 5, now=T0) == 0 and not store.is_player(storage, 5)  # no greenhouse, no sunlight
    store.get_state(storage, 5, now=T0)
    cap, start = RULES["sunlight"]["daily_cap"], RULES["starting"]["sunlight"]
    assert sum(store.add_sunlight(storage, 5, now=T0 + n) for n in range(cap + 10)) == cap
    state = store.get_state(storage, 5, now=T0 + 100)
    assert state["sunlight"] == start + cap and state["sun_today"] == cap
    tomorrow = T0 + 86400
    assert store.get_state(storage, 5, now=tomorrow)["sun_today"] == 0 and store.add_sunlight(storage, 5, now=tomorrow) == 1
    with store.connect(storage) as connection:  # storage has a ceiling too
        connection.execute("UPDATE garden_players SET sunlight = ? WHERE user_id = 5", (RULES["sunlight"]["storage_cap"],))
    assert store.add_sunlight(storage, 5, now=tomorrow + 1) == 0
    assert store.player_ids(storage) == {5}


def test_the_speed_setting_shortens_every_wait(storage, monkeypatch):
    monkeypatch.setenv("GARDEN_SPEED", "60")
    store.get_state(storage, 1, now=T0)
    state, _ = store.collect_water(storage, 1, now=T0)
    assert state["barrel"]["next_at"] == T0 + PER_WATER / 60
    store.plant_seed(storage, 1, "chamomile", now=T0)
    assert store.water_plant(storage, 1, now=T0)[0]["plant"]["ready_at"] == T0 + RULES["grow_seconds"][0] / 60
    monkeypatch.setenv("GARDEN_SPEED", "nonsense")
    assert rules.speed() == 1.0


def test_the_words_and_the_summary_read_naturally(storage):
    state = store.get_state(storage, 1, now=T0)
    assert "rain barrel is full" in describe(state) and "Pick a seed" in describe(state)
    store.plant_seed(storage, 1, "tulip", now=T0)
    state, _ = store.water_plant(storage, 1, now=T0)
    assert f"<t:{int(T0 + RULES['grow_seconds'][0]) + 1}:R>" in describe(state) and "**Tulip** (seed) is growing" in describe(state)
    assert summarize(["collected 8 water"]) == "collected 8 water"
    assert summarize(["collected 8 water", "watered their Tulip", "watered their Tulip"]) == "collected 8 water and watered their Tulip (2 times)"
    assert summarize(["a", "b", "c"]) == "a, b and c"


@needs_art
def test_every_species_and_stage_draws_a_sharp_picture(storage):
    state = store.get_state(storage, 1, now=T0)
    empty = render.render_greenhouse(state)
    picture = Image.open(io.BytesIO(empty))
    assert picture.format == "PNG" and picture.size == tuple(edge * render.SCALE for edge in render.image("greenhouse").size) == (648, 432)
    seen = {empty}
    for species in RULES["species"]:
        for stage in range(rules.MATURE_STAGE + 1):
            plant = {"species": species, "stage": stage, "mature": stage >= rules.MATURE_STAGE, "growing": False, "needs_water": stage < rules.MATURE_STAGE}
            pots = [{"index": 0, "locked": False, "plant": plant}, *state["pots"][1:]]
            seen.add(render.render_greenhouse({**state, "pots": pots}))
    # stage 0 is a seed under the soil and looks the same for every species; everything else differs
    assert len(seen) == 1 + 1 + len(RULES["species"]) * rules.MATURE_STAGE
    assert set(RULES["starters"]) <= set(RULES["species"]) <= {k for k in render._manifest()["species"] if not k.startswith("_")}


@needs_art
def test_the_website_reaches_the_same_greenhouse_through_the_bridge(storage, monkeypatch):
    token = "t" * 40
    auth = {"Authorization": f"Bearer {token}"}
    garden = SimpleNamespace(players=set())
    bot = SimpleNamespace(storage_path=storage, guilds=[], voice_clients=[], get_guild=lambda _id: None, get_cog=lambda name: garden if name == "Garden" else None)

    async def run():
        cog = PanelBridge(bot)
        cog._token = token
        async with TestClient(TestServer(cog.build_app())) as client:
            state = (await (await client.get("/v1/users/7/greenhouse", headers=auth)).json())["state"]
            assert state["new"] and state["user_id"] == "7" and garden.players == {7}
            image = await client.get("/v1/users/7/greenhouse/image", headers=auth)
            assert image.status == 200 and image.content_type == "image/png" and (await image.read())[:4] == b"\x89PNG"
            done = await (await client.post("/v1/users/7/greenhouse/action", headers=auth, json={"action": "plant", "species": "tulip"})).json()
            assert done["did"] == "planted a Tulip seed" and done["state"]["plant"]["species"] == "tulip"
            refused = await client.post("/v1/users/7/greenhouse/action", headers=auth, json={"action": "bench"})
            assert refused.status == 409 and "fully grown" in (await refused.json())["error"]
            assert (await client.get("/v1/users/abc/greenhouse", headers=auth)).status == 400
            monkeypatch.setenv("GARDEN_ENABLED", "0")  # a bot without the game answers as if the route did not exist
            assert (await client.get("/v1/users/7/greenhouse", headers=auth)).status == 404

    asyncio.run(run())


@needs_art
def test_a_discord_visit_is_private_and_leaves_one_public_picture(storage):
    from unittest.mock import AsyncMock

    from cogs.garden import Garden, GreenhouseView
    from utils.embeds import EmbedManager

    user = SimpleNamespace(id=9, display_name="Fern", mention="<@9>")

    def interaction(values=None):
        return SimpleNamespace(
            user=user, guild=SimpleNamespace(id=1), data={"values": values or []},
            response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
        )

    async def run():
        bot = SimpleNamespace(storage_path=storage, embeds=EmbedManager())
        cog = Garden(bot)
        first = interaction()
        await Garden.greenhouse.callback(cog, first)
        sent = first.response.send_message.await_args.kwargs
        view: GreenhouseView = sent["view"]
        assert sent["ephemeral"] and sent["file"].filename == "greenhouse.png" and "Welcome" in sent["embed"].description and cog.players == {9}
        assert [getattr(item, "label", None) or item.placeholder for item in view.children] == ["Pick a seed to plant", "Collect water (8)", "Refresh", "Done"]

        press = interaction()
        await view._do(press, "collect")
        await view._do(interaction(["tulip"]), "plant", species="tulip")
        watered = interaction()
        await view._do(watered, "water")
        labels = [item.label for item in view.children]
        assert labels == ["Collect water", "Water plant", "Refresh", "Done"] and view.children[0].disabled and view.children[1].disabled
        refused = interaction()
        await view._do(refused, "water")  # already growing: the panel says so and nothing is added to the summary
        assert "already growing" in refused.response.edit_message.await_args.kwargs["embed"].description
        assert press.response.edit_message.await_args.kwargs["attachments"][0].filename == "greenhouse.png"

        done = interaction()
        await view._done(done)
        assert done.edit_original_response.await_args.kwargs["view"] is None
        posted = done.followup.send.await_args.kwargs
        assert posted["embed"].title == "Fern tended their greenhouse" and posted["file"].filename == "greenhouse.png"
        assert posted["embed"].description == "<@9> collected 8 water, planted a Tulip seed and watered their Tulip.\nTheir **Tulip** is at the seed stage."
        await view._close()  # closing twice posts nothing more
        assert done.followup.send.await_count == 1

        # looking without doing anything, or playing in a DM, posts nothing
        quiet = interaction()
        await Garden.greenhouse.callback(cog, quiet)
        await quiet.response.send_message.await_args.kwargs["view"]._close()
        assert quiet.followup.send.await_count == 0 and quiet.edit_original_response.await_count == 1

    asyncio.run(run())
