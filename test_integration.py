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
    "300 g Brokkoli, in Röschen",
]
STEPS = [
    "1 Zwiebel, halbiert und 2 Knoblauchzehen in den Mixtopf geben.",
    "Zerkleinern 5 Sek./Stufe 5.",
    "Mit dem Spatel nach unten schieben und 20 g Olivenöl zugeben.",
    "Andünsten 3 Min./120°C/Linkslauf/Stufe 1.",
    "200 g Langkornreis, 400 g Wasser und 200 g stückige Tomaten zugeben. Varoma aufsetzen und 300 g Brokkoli, in Röschen hineinlegen.",
    "Kochen 18 Min./100°C/Linkslauf/Stufe 1.",
    "Dämpfen 15 Min./Varoma/Stufe 2.",
]

# Expected action elements in document order:
#   (tag, attribute_checks_dict)
# tag is "cr-tts" for standard cook or "cr-mode" for mode annotations (e.g. STEAMING).
EXPECTED_ACTIONS = [
    ("cr-tts", {"time": "5", "time-unit": "s", "speed": "5", "-no-temp": True}),
    ("cr-tts", {"time": "180", "time-unit": "s", "speed": "1", "temperature": "120", "temperature-unit": "C"}),
    ("cr-tts", {"time": "1080", "time-unit": "s", "speed": "1", "temperature": "100", "temperature-unit": "C"}),
    ("cr-mode", {"time": "900", "time-unit": "s", "speed": "2", "name": "steaming", "accessory": "Varoma"}),
]

EXPECTED_MIN_INGREDIENTS = 7


def _extract_tag_attrs(html: str, tag: str) -> list[dict]:
    tags = re.findall(rf"<{tag}\s([^>]*)>", html)
    return [dict(re.findall(r'(\w[\w-]*)\s*=\s*"([^"]*)"', raw)) for raw in tags]


def extract_action_tags(html: str) -> list[tuple[str, dict]]:
    """Extract all cr-tts and cr-mode opening tags as (tag_name, attrs). Preserves document order."""
    result = []
    for m in re.finditer(r"<(cr-tts|cr-mode)\s([^>]*)>", html):
        tag = m.group(1)
        raw = m.group(2)
        attrs = dict(re.findall(r'(\w[\w-]*)\s*=\s*"([^"]*)"', raw))
        result.append((tag, attrs))
    return result


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


async def _assert_rollback(svc: CookidooService) -> None:
    """Trigger a deliberate PATCH failure and verify the partial recipe was rolled back."""
    before = {r["recipe_id"] for r in await svc.list_custom_recipes()}
    try:
        await svc.create_custom_recipe(
            name=f"[ROLLBACK-TEST {int(time.time())}]",
            ingredients=["1 g nothing"],
            # Intentionally invalid TTS temperature value (enum rejects "99999"):
            steps=["Kochen 1 Min./99999°C/Stufe 1."],
            servings=1, prep_time=1, total_time=1, hints=None, tools=["TM7"],
        )
        raise AssertionError("rollback test: expected PATCH to fail, but it succeeded")
    except Exception as e:
        if "rollback test" in str(e):
            raise
        # Expected: PATCH 400, rollback deleted the empty recipe
        after = {r["recipe_id"] for r in await svc.list_custom_recipes()}
        new_recipes = after - before
        if new_recipes:
            raise AssertionError(
                f"rollback failed — zombie recipe(s) remain: {new_recipes}"
            )
    print("  OK  failed upload rolled back cleanly (no zombie)")


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

        print("3. Asserting rendered action tags (cr-tts + cr-mode) ...")
        actions = extract_action_tags(html)
        _assert(
            len(actions) >= len(EXPECTED_ACTIONS),
            f"found {len(actions)} action tag(s), expected at least {len(EXPECTED_ACTIONS)}",
        )

        for i, (exp_tag, exp_attrs) in enumerate(EXPECTED_ACTIONS):
            actual_tag, attrs = actions[i]
            _assert(
                actual_tag == exp_tag,
                f"action[{i}] tag={actual_tag!r} (expected {exp_tag!r})",
            )
            for key, expected in exp_attrs.items():
                if key == "-no-temp":
                    _assert(
                        "temperature" not in attrs,
                        f"action[{i}] must NOT have temperature (got {attrs.get('temperature')!r})",
                    )
                    continue
                _assert(
                    attrs.get(key) == expected,
                    f"action[{i}] {key}={attrs.get(key)!r} (expected {expected!r})",
                )

        print("4. Asserting ingredient annotations ...")
        n_ing = count_cr_ingredient(html)
        _assert(
            n_ing >= EXPECTED_MIN_INGREDIENTS,
            f"found {n_ing} cr-ingredient tag(s), expected at least {EXPECTED_MIN_INGREDIENTS}",
        )

        print("5. Asserting create_custom_recipe rollback on PATCH failure ...")
        await _assert_rollback(svc)

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
