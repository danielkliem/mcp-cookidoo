"""
Integration test: verify that create_custom_recipe produces recipes where the
Cookidoo backend actually renders <cr-tts> tags with time/speed/temperature
attributes — i.e. they will show a Play-Button on the TM7 device.

Hits the real Cookidoo API; needs COOKIDOO_EMAIL / COOKIDOO_PASSWORD in .env.
Creates a uniquely-named test recipe, verifies its edit-page HTML, and deletes
the recipe at the end (even on failure). Exits 0 on pass, non-zero on fail.

Run: ./venv/bin/python test_integration.py
"""

import asyncio
import re
import sys
import time
from typing import Optional

from cookidoo_service import CookidooService, load_cookidoo_credentials


TEST_NAME = f"[INT-TEST {int(time.time())}] TTS Annotation Roundtrip"

INGREDIENTS = [
    "1 Zwiebel, halbiert",
    "2 Knoblauchzehen",
    "20 g Olivenöl",
    "200 g Langkornreis",
    "400 g Wasser",
    "200 g stückige Tomaten",
]
STEPS = [
    "1 Zwiebel, halbiert und 2 Knoblauchzehen in den Mixtopf geben.",
    "Zerkleinern 5 Sek./Stufe 5.",
    "Mit dem Spatel nach unten schieben und 20 g Olivenöl zugeben.",
    "Andünsten 3 Min./120°C/Linkslauf/Stufe 1.",
    "200 g Langkornreis, 400 g Wasser und 200 g stückige Tomaten zugeben.",
    "Kochen 18 Min./100°C/Linkslauf/Stufe 1.",
]

# (time, time-unit, speed, temperature or None) — order matches the 3 action steps
EXPECTED_TTS = [
    ("5", "s", "5", None),
    ("180", "s", "1", "120"),
    ("1080", "s", "1", "100"),
]

# Minimum number of ingredient annotations we expect to be rendered.
EXPECTED_MIN_INGREDIENTS = 6


def extract_cr_tts_attrs(html: str) -> list[dict]:
    """Extract attributes from every <cr-tts ...> opening tag on the edit-steps page."""
    tags = re.findall(r"<cr-tts\s([^>]*)>", html)
    attrs_list = []
    for raw in tags:
        attrs = dict(re.findall(r'(\w[\w-]*)\s*=\s*"([^"]*)"', raw))
        attrs_list.append(attrs)
    return attrs_list


def count_cr_ingredient(html: str) -> int:
    return len(re.findall(r"<cr-ingredient", html))


async def _fetch_edit_steps_html(svc: CookidooService, recipe_id: str) -> str:
    api = svc.api_client
    assert api is not None
    loc = api.localization
    base_url = "https://" + loc.url.split("/")[2]
    url = (
        f"{base_url}/created-recipes/{loc.language}/{recipe_id}"
        "/edit/ingredients-and-preparation-steps?active=steps"
    )
    headers = {
        "Accept": "text/html",
        "Authorization": f"Bearer {api.auth_data.access_token}",
    }
    async with api._session.get(url, headers=headers) as r:
        r.raise_for_status()
        return await r.text()


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  OK  {msg}")


async def run() -> int:
    email, password = load_cookidoo_credentials()
    svc = CookidooService(email, password)
    api = await svc.login()

    recipe_id: Optional[str] = None
    try:
        print(f"1. Uploading test recipe '{TEST_NAME}' ...")
        recipe_id = await svc.create_custom_recipe(
            name=TEST_NAME,
            ingredients=INGREDIENTS,
            steps=STEPS,
            servings=4,
            prep_time=5,
            total_time=30,
            hints=None,
            tools=["TM7", "TM6", "TM5"],
        )
        print(f"   -> recipe_id = {recipe_id}")

        print("2. Fetching edit-steps HTML ...")
        html = await _fetch_edit_steps_html(svc, recipe_id)

        print("3. Asserting rendered cr-tts tags ...")
        tts_attrs = extract_cr_tts_attrs(html)
        _assert(
            len(tts_attrs) >= len(EXPECTED_TTS),
            f"found {len(tts_attrs)} cr-tts tag(s), expected at least {len(EXPECTED_TTS)}",
        )

        for i, (exp_time, exp_unit, exp_speed, exp_temp) in enumerate(EXPECTED_TTS):
            attrs = tts_attrs[i]
            _assert(
                attrs.get("time") == exp_time,
                f"cr-tts[{i}] time={attrs.get('time')!r} (expected {exp_time!r})",
            )
            _assert(
                attrs.get("time-unit") == exp_unit,
                f"cr-tts[{i}] time-unit={attrs.get('time-unit')!r} (expected {exp_unit!r})",
            )
            _assert(
                attrs.get("speed") == exp_speed,
                f"cr-tts[{i}] speed={attrs.get('speed')!r} (expected {exp_speed!r})",
            )
            if exp_temp is None:
                _assert(
                    "temperature" not in attrs,
                    f"cr-tts[{i}] must NOT have temperature (got {attrs.get('temperature')!r})",
                )
            else:
                _assert(
                    attrs.get("temperature") == exp_temp,
                    f"cr-tts[{i}] temperature={attrs.get('temperature')!r} (expected {exp_temp!r})",
                )
                _assert(
                    attrs.get("temperature-unit") == "C",
                    f"cr-tts[{i}] temperature-unit={attrs.get('temperature-unit')!r} (expected 'C')",
                )

        print("4. Asserting ingredient annotations ...")
        n_ing = count_cr_ingredient(html)
        _assert(
            n_ing >= EXPECTED_MIN_INGREDIENTS,
            f"found {n_ing} cr-ingredient tag(s), expected at least {EXPECTED_MIN_INGREDIENTS}",
        )

        print("\nPASS — all assertions satisfied; recipe will render with Play-Buttons on TM7.")
        return 0

    except AssertionError as e:
        print(f"\nFAIL — {e}")
        return 1
    except Exception as e:
        print(f"\nERROR — {type(e).__name__}: {e}")
        return 2
    finally:
        if recipe_id:
            print(f"\nCleanup: deleting test recipe {recipe_id} ...")
            try:
                await api.remove_custom_recipe(recipe_id)
                print("   -> deleted")
            except Exception as e:
                print(f"   -> cleanup failed: {e}")
        await svc.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
