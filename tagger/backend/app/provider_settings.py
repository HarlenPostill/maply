"""Local provider preferences. Secrets never appear in public settings responses."""
from __future__ import annotations

import json
import os
import tempfile
import threading

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
import httpx

from app.config import get_settings

router = APIRouter(prefix='/api/tagging/provider')
_lock = threading.Lock()
DEFAULT_OPENROUTER_MODEL = 'google/gemini-3.1-flash-lite'


def load_settings() -> dict:
    path = get_settings().data_root / 'provider-settings.json'
    raw = json.loads(path.read_text()) if path.exists() else {}
    # Read older preference files without retaining removed local-provider settings.
    return {'provider': 'openrouter', 'openrouter_model': raw.get('openrouter_model', DEFAULT_OPENROUTER_MODEL),
            'api_key': raw.get('api_key', ''), 'configured': bool(raw.get('api_key'))}


def public_settings(value: dict) -> dict:
    return {'openrouter_model': value['openrouter_model'], 'api_key_configured': bool(value.get('api_key'))}


class ProviderUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    openrouter_model: str = Field(min_length=1, max_length=100, pattern=r'^[^\s/]+/[^\s]+$')
    api_key: SecretStr | None = None
    clear_api_key: bool = False


@router.get('')
def read_provider(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return public_settings(load_settings())


@router.put('')
def update_provider(req: ProviderUpdate, response: Response):
    with _lock:
        value = load_settings()
        key = req.api_key.get_secret_value().strip() if req.api_key is not None else ''
        if key and (len(key) > 512 or any(c.isspace() for c in key) or not key.isascii()):
            raise HTTPException(422, 'API key must be ASCII without spaces and at most 512 characters.')
        value.update(req.model_dump(exclude={'api_key', 'clear_api_key'}))
        if req.clear_api_key:
            value['api_key'] = ''
        elif key:
            value['api_key'] = key
        value['configured'] = True
        root = get_settings().data_root
        root.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.provider-', dir=root)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(value, output)
            os.replace(name, root / 'provider-settings.json')
        finally:
            if os.path.exists(name):
                os.unlink(name)
    response.headers['Cache-Control'] = 'no-store'
    return public_settings(value)


@router.get('/models')
def openrouter_models():
    try:
        result = httpx.get('https://openrouter.ai/api/v1/models', timeout=15, follow_redirects=False)
        result.raise_for_status()
        models = result.json()['data']
        return {'models': sorted([
            {'id': m['id'], 'name': m.get('name', m['id'])}
            for m in models
            if 'image' in m.get('architecture', {}).get('input_modalities', [])
            and 'text' in m.get('architecture', {}).get('output_modalities', [])
            and 'structured_outputs' in m.get('supported_parameters', [])
            and ':batch' not in m['id']
        ], key=lambda m: m['name'])}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(502, 'Cannot load OpenRouter models. You can still enter a model ID manually.')
