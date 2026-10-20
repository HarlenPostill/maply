"""Provider preferences and OpenRouter's HTTP boundary, without paid API calls."""
import json
import stat

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import provider_settings as settings
from app import openrouter_provider as provider
from app.vision_provider import VisionProviderError


@pytest.fixture
def client(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(settings, 'get_settings', lambda: SimpleNamespace(data_root=tmp_path))
    app = FastAPI()
    app.include_router(settings.router)
    with TestClient(app) as client:
        yield client


def payload(**changes):
    return dict(provider='openrouter', antigravity_model='local-model',
                openrouter_model='google/gemini-3.1-flash-lite', **changes)


def test_key_is_private_persisted_and_preserved(client, tmp_path):
    assert client.get('/api/tagging/provider').json()['provider'] == 'antigravity'
    response = client.put('/api/tagging/provider', json=payload(api_key='test-private-key'))
    assert response.status_code == 200
    assert response.json()['api_key_configured']
    assert 'test-private-key' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    path = tmp_path / 'provider-settings.json'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())['api_key'] == 'test-private-key'
    assert client.put('/api/tagging/provider', json=payload()).status_code == 200
    assert settings.load_settings()['api_key'] == 'test-private-key'
    assert 'test-private-key' not in client.get('/api/tagging/provider').text
    changed = payload(clear_api_key=True)
    changed['provider'] = 'antigravity'
    assert client.put('/api/tagging/provider', json=changed).status_code == 200
    assert not settings.load_settings()['api_key']


def test_openrouter_requires_key_and_rejected_changes_are_atomic(client):
    assert client.put('/api/tagging/provider', json=payload()).status_code == 422
    assert client.get('/api/tagging/provider').json()['provider'] == 'antigravity'
    client.put('/api/tagging/provider', json=payload(api_key='saved-key'))
    assert client.put('/api/tagging/provider', json=payload(clear_api_key=True)).status_code == 422
    assert settings.load_settings()['api_key'] == 'saved-key'


def test_catalog_filters_for_image_and_structured_output(client, monkeypatch):
    monkeypatch.setattr(settings.httpx, 'get', lambda *a, **kw: httpx.Response(200,
        request=httpx.Request('GET', a[0]), json={'data': [
            {'id': 'test/vision', 'architecture': {'input_modalities': ['image'], 'output_modalities': ['text']}, 'supported_parameters': ['structured_outputs']},
            {'id': 'test/text', 'architecture': {'input_modalities': ['text'], 'output_modalities': ['text']}, 'supported_parameters': ['structured_outputs']},
        ]}))
    assert client.get('/api/tagging/provider/models').json() == {'models': [{'id': 'test/vision', 'name': 'test/vision'}]}


def mock_openrouter(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(provider.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))


def test_image_schema_tokens_and_output(monkeypatch, tmp_path):
    image = tmp_path / 'photo.jpg'
    image.write_bytes(b'image-bytes')
    calls, events = [], []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"features":[]}'}}],
                                         'usage': {'prompt_tokens': 50, 'completion_tokens': 20, 'total_tokens': 70}})
    mock_openrouter(monkeypatch, respond)
    assert provider.annotate_openrouter(image, 'inspect', {'type': 'object'}, 'test/model', api_key='private-key', on_event=events.append) == {'features': []}
    data = json.loads(calls[0].content)
    assert calls[0].url == 'https://openrouter.ai/api/v1/chat/completions'
    assert calls[0].headers['authorization'] == 'Bearer private-key'
    assert data['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
    assert data['response_format']['json_schema']['schema'] == {'type': 'object'}
    assert next(e for e in events if e['kind'] == 'usage')['data']['usage']['total_tokens'] == 70
    assert 'private-key' not in json.dumps(events)


@pytest.mark.parametrize('status', [401, 402, 429, 500])
def test_errors_do_not_leak_provider_body(monkeypatch, tmp_path, status):
    image = tmp_path / 'photo.jpg'
    image.write_bytes(b'image')
    mock_openrouter(monkeypatch, lambda req: httpx.Response(status, text='private-key and account details'))
    with pytest.raises(VisionProviderError) as caught:
        provider.annotate_openrouter(image, 'inspect', {}, 'test/model', api_key='private-key')
    assert str(status) in str(caught.value)
    assert 'private-key' not in str(caught.value)


def test_truncated_annotation_is_rejected(monkeypatch, tmp_path):
    image = tmp_path / 'photo.jpg'
    image.write_bytes(b'image')
    mock_openrouter(monkeypatch, lambda req: httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}))
    with pytest.raises(VisionProviderError, match='did not finish'):
        provider.annotate_openrouter(image, 'inspect', {}, 'test/model', api_key='key')


def test_compact_schema_retains_observation_shape_without_changing_cli_schema():
    import copy
    from app.tag_schema import cli_json_schema
    original = cli_json_schema()
    before = copy.deepcopy(original)
    compact = provider.openrouter_schema(original)
    assert original == before
    tags = compact['properties']['features']['items']['properties']['tags']
    assert 'properties' not in tags
    observation = tags['additionalProperties']
    assert observation['properties']['value'] == {'type': 'string'}
    assert set(observation['required']) == {'value', 'confidence', 'evidence', 'visibility'}
    assert observation['additionalProperties'] is False
    assert compact['properties']['footpath'] == original['properties']['footpath']
    assert len(json.dumps(compact)) < len(json.dumps(original)) / 2


def test_compact_schema_does_not_relax_server_vocabulary_validation():
    from pydantic import ValidationError
    from app.tag_schema import LabelResult
    observation = {'value': 'present', 'confidence': .9, 'evidence': 'Visible path', 'visibility': 'clear'}
    labels = {'schema_version': 'maply-accessibility/1', 'footpath': observation,
              'features': [{'id': 'f1', 'kind': 'footpath_section', 'region': None,
                            'location_description': 'Near side', 'tags': {'invented_tag': observation}}],
              'notes': [], 'review_reasons': []}
    with pytest.raises(ValidationError, match='unsupported footpath_section tag'):
        LabelResult.model_validate(labels)
