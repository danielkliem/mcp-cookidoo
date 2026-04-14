"""
Cookidoo Service

Module to encapsulate all cookidoo-api logic for interacting with the Cookidoo platform.
"""

import os
import re
from typing import Optional
from dotenv import load_dotenv
from aiohttp import ClientSession
from cookidoo_api import Cookidoo, CookidooConfig
from cookidoo_api.helpers import (
    get_localization_options,
)
import aiohttp
import asyncio


# --- TTS parsing / annotation building ---

_TTS_SPAN_RE = re.compile(
    r"(\d+\s*(?:Sek\.?|sec|Min\.?|min)"                    # time (required)
    r"(?:\s*/\s*(?:\d+\s*°\s*C|Varoma))?"                   # optional temp
    r"(?:\s*/\s*(?:Linkslauf|sens inverse|reverse))?"       # optional direction
    r"\s*/\s*Stufe\s*\d+(?:[.,]\d+)?)",                    # speed (required)
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"(\d+)\s*(Sek\.?|sec|Min\.?|min)", re.IGNORECASE)
_SPEED_RE = re.compile(r"Stufe\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_TEMP_RE = re.compile(r"(\d+)\s*°\s*C|([Vv]aroma)")


def _parse_action(text: str) -> Optional[tuple[str, Optional[str], dict]]:
    """
    Parse a TM action span into (annotation_type, annotation_name, data).

    - Standard cooking ('5 Sek./Stufe 5', '3 Min./120°C/Linkslauf/Stufe 1')
      → ("TTS", None, {time, speed, temperature?})
    - Varoma steaming ('15 Min./Varoma/Stufe 2')
      → ("MODE", "STEAMING", {time, speed, direction, accessory})
    """
    m_time = _TIME_RE.search(text)
    if not m_time:
        return None
    n = int(m_time.group(1))
    unit = m_time.group(2).lower()
    if unit.startswith("sek") or unit.startswith("sec"):
        seconds = n
    elif unit.startswith("min"):
        seconds = n * 60
    else:
        return None

    m_speed = _SPEED_RE.search(text)
    if not m_speed:
        return None
    speed = m_speed.group(1).replace(",", ".")

    m_temp = _TEMP_RE.search(text)
    is_varoma = bool(m_temp and m_temp.group(2))

    if is_varoma:
        return (
            "MODE",
            "STEAMING",
            {
                "time": seconds,
                "speed": speed,
                "direction": "CW",
                "accessory": "Varoma",
            },
        )

    data: dict = {"speed": speed, "time": seconds}
    if m_temp:
        data["temperature"] = {"value": m_temp.group(1), "unit": "C"}
    return ("TTS", None, data)


def build_step_annotations(text: str, ingredients: list[str]) -> list[dict]:
    """
    Build TTS + INGREDIENT annotations for a single step text.

    Detects TM action patterns (time/temp/speed combos) and matches ingredient
    references from the given ingredients list using exact-substring matching.
    Overlapping matches are avoided; longest ingredients match first.
    """
    annotations: list[dict] = []
    used: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(not (end <= a or start >= b) for a, b in used)

    for m in _TTS_SPAN_RE.finditer(text):
        start, end = m.span(1)
        parsed = _parse_action(m.group(1))
        if parsed is None or overlaps(start, end):
            continue
        atype, aname, data = parsed
        annotation: dict = {
            "type": atype,
            "data": data,
            "position": {"offset": start, "length": end - start},
        }
        if aname is not None:
            annotation["name"] = aname
        annotations.append(annotation)
        used.append((start, end))

    for ing in sorted((i for i in ingredients if i), key=len, reverse=True):
        search_from = 0
        while True:
            idx = text.find(ing, search_from)
            if idx < 0:
                break
            end = idx + len(ing)
            if overlaps(idx, end):
                search_from = idx + 1
                continue
            annotations.append(
                {
                    "type": "INGREDIENT",
                    "data": {"description": ing},
                    "position": {"offset": idx, "length": len(ing)},
                }
            )
            used.append((idx, end))
            search_from = end

    annotations.sort(key=lambda a: a["position"]["offset"])
    return annotations


def build_instruction(text: str, ingredients: list[str]) -> dict:
    return {
        "type": "STEP",
        "text": text,
        "annotations": build_step_annotations(text, ingredients),
    }


def load_cookidoo_credentials() -> tuple[str, str]:
    """
    Load Cookidoo credentials from .env file.
    
    Returns:
        tuple[str, str]: Email and password
        
    Raises:
        ValueError: If credentials are not found in environment variables
    """
    load_dotenv()
    
    email = os.getenv("COOKIDOO_EMAIL")
    password = os.getenv("COOKIDOO_PASSWORD")
    
    if not email or not password:
        raise ValueError(
            "Missing Cookidoo credentials. Please set COOKIDOO_EMAIL and "
            "COOKIDOO_PASSWORD in your .env file"
        )
    
    return email, password


class CookidooService:
    """Service class for managing Cookidoo API interactions."""
    
    def __init__(self, email: str, password: str):
        """
        Initialize the Cookidoo service with credentials.
        
        Args:
            email: Cookidoo account email
            password: Cookidoo account password
        """
        self.email = email
        self.password = password
        self._api_client: Optional[Cookidoo] = None
        self._session: Optional[ClientSession] = None
    
    async def login(self) -> Cookidoo:
        """
        Authenticate with Cookidoo and return the API client.
        
        Returns:
            Cookidoo: Authenticated Cookidoo API client
            
        Raises:
            Exception: If authentication fails
        """
        try:
            # Create aiohttp ClientSession with a timeout
            self._session = ClientSession(connector=aiohttp.TCPConnector(verify_ssl=False))
            

            # Create CookidooConfig with credentials
            config = CookidooConfig(
                email=self.email,
                password=self.password,
                localization=(
                    await get_localization_options(country="ch", language="de-CH")
                )[0],
            )
            
            # Create Cookidoo API client with session and config
            self._api_client = Cookidoo(session=self._session, cfg=config)
            
            # Perform login (no parameters needed - uses config)
            await self._api_client.login()
            
            return self._api_client
            
        except Exception as e:
            # Clean up session if login fails
            if self._session:
                await self._session.close()
            raise Exception(f"Failed to authenticate with Cookidoo: {str(e)}") from e
    
    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session:
            await self._session.close()
    
    async def create_custom_recipe(
        self,
        name: str,
        ingredients: list[str],
        steps: list[str],
        servings: int = 4,
        prep_time: int = 30,
        total_time: int = 60,
        hints: Optional[list[str]] = None,
        tools: Optional[list[str]] = None,
    ) -> str:
        """
        Create a completely new custom recipe from scratch using the undocumented API.
        
        Args:
            name: Recipe name
            ingredients: List of ingredient descriptions
            steps: List of cooking step descriptions
            servings: Number of servings (default: 4)
            prep_time: Preparation time in minutes (default: 30)
            total_time: Total cooking time in minutes (default: 60)
            hints: Optional list of hints/tips for the recipe
            
        Returns:
            str: The created recipe ID
            
        Raises:
            Exception: If recipe creation fails
        """
        if not self._api_client or not self._session:
            raise Exception("Not authenticated. Please call login() first.")
        
        try:
            # Get the access token from the authenticated client
            auth_data = self._api_client.auth_data
            if not auth_data:
                raise Exception("No authentication data available")
            
            localization = self._api_client.localization
            # Extract base domain from the URL (e.g., "https://cookidoo.fr/foundation/fr-FR" -> "https://cookidoo.fr")
            url_parts = localization.url.split("/")
            base_url = f"{url_parts[0]}//{url_parts[2]}"  # protocol + domain
            locale = localization.language 
            
            # Headers for the undocumented API
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_client.auth_data.access_token}"
            }
            
            # Use the API client's session to ensure cookies are shared
            api_session = self._api_client._session
        
            
            # Step 1: Create the recipe with just the name
            create_url = f"{base_url}/created-recipes/{locale}"
            create_data = {"recipeName": name}
            
            async with api_session.post(
                create_url, json=create_data, headers=headers
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(
                        f"Failed to create recipe. Status: {response.status}, Error: {error_text}"
                    )
                
                result = await response.json()
                recipe_id = result.get("recipeId")
                
                if not recipe_id:
                    raise Exception("No recipe ID returned from creation")
            
            # Step 2: Update recipe with ingredients
            update_url = f"{base_url}/created-recipes/{locale}/{recipe_id}"
            
            # PATCH requires a complete recipe structure with ALL required fields
            update_data = {
                "name": name,
                "image": None,  # Can be null or match pattern: ^((prod|nonprod)/img/customer-recipe/)?[A-Za-z0-9-_]{1,}.(bmp|jpe|jpeg|jpg|png)$
                "isImageOwnedByUser": False,
                "tools": tools if tools else ["TM7", "TM6", "TM5"],
                "yield": {"value": servings, "unitText": "portion"},
                "prepTime": prep_time * 60,  # Convert minutes to seconds
                "cookTime": 0,
                "totalTime": total_time * 60,  # Convert minutes to seconds
                "ingredients": [{"type": "INGREDIENT", "text": ing} for ing in ingredients],
                "instructions": [build_instruction(step, ingredients) for step in steps],
                "hints": "\n".join(hints) if hints and isinstance(hints, list) else (hints if hints else ""),
                "workStatus": "PRIVATE",
                "recipeMetadata": {
                    "requiresAnnotationsCheck": False
                }
            }
            
            await asyncio.sleep(5)

            try:
                async with api_session.patch(update_url, json=update_data, headers=headers) as response:
                    response_text = await response.text()
                    if response.status not in [200, 204]:
                        raise Exception(
                            f"Failed to update recipe. Status: {response.status}, Error: {response_text}"
                        )
            except Exception:
                # Rollback: the POST created an empty recipe; delete it so no zombie remains.
                try:
                    await self._api_client.remove_custom_recipe(recipe_id)
                except Exception:
                    pass
                raise

            return recipe_id

        except Exception as e:
            raise Exception(f"Failed to create custom recipe: {str(e)}") from e

    async def delete_custom_recipe(self, recipe_id: str) -> None:
        """Delete one of the user's custom recipes by ID."""
        if not self._api_client:
            raise Exception("Not authenticated. Please call login() first.")
        await self._api_client.remove_custom_recipe(recipe_id)

    async def list_custom_recipes(self) -> list[dict]:
        """List the user's custom recipes. Returns a list of {recipe_id, name, created_at, total_time}."""
        if not self._api_client or not self._session:
            raise Exception("Not authenticated. Please call login() first.")
        localization = self._api_client.localization
        url_parts = localization.url.split("/")
        base_url = f"{url_parts[0]}//{url_parts[2]}"
        url = f"{base_url}/created-recipes/{localization.language}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_client.auth_data.access_token}",
        }
        async with self._api_client._session.get(url, headers=headers) as response:
            if response.status != 200:
                raise Exception(f"Failed to list recipes. Status: {response.status}")
            data = await response.json()
        items = []
        for item in data.get("items", []):
            content = item.get("recipeContent", {})
            items.append(
                {
                    "recipe_id": item.get("recipeId"),
                    "name": content.get("name"),
                    "created_at": item.get("createdAt"),
                    "total_time": content.get("totalTime"),
                    "servings": content.get("recipeYield", {}).get("value"),
                }
            )
        return items
    
    @property
    def api_client(self) -> Optional[Cookidoo]:
        """Get the current API client instance."""
        return self._api_client
