"""Provider adapters for the model gateway: wire shapes per provider class.

An *operation* is a wire protocol (image, transcription, speech, chat). A
capability is routed to an operation by its built-in name, or by the route's
explicit ``operation``. A provider class lists the operations it implements;
an operator may narrow a provider further with ``capabilities``. Anything else
is an unsupported modality, refused before any request.

Wire shapes were checked against OpenRouter's documentation on 2026-09-30:
- images: POST /api/v1/images, response ``data[].b64_json`` + ``media_type``;
- speech: POST /api/v1/audio/speech, raw audio bytes;
- transcription: POST /api/v1/audio/transcriptions, JSON ``input_audio``;
- chat: POST /api/v1/chat/completions.
``openai-compatible`` uses the OpenAI paths (``/images/generations``,
multipart ``/audio/transcriptions``). See docs/MODEL-GATEWAY.md for sources.
"""
from __future__ import annotations

import base64
import binascii
import json
import math

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.model_gateway_http import ProviderCallFailed


OPERATIONS = ('image', 'transcription', 'speech', 'chat')

# The initial capability set and the operation each one uses.
DEFAULT_OPERATIONS = {
    'image.generate': 'image',
    'audio.transcribe': 'transcription',
    'audio.speak': 'speech',
    'text.complete': 'chat',
}

PROVIDER_PATHS = {
    'openrouter': {'image': '/images', 'transcription': '/audio/transcriptions',
                   'speech': '/audio/speech', 'chat': '/chat/completions'},
    'openai-compatible': {'image': '/images/generations', 'transcription': '/audio/transcriptions',
                          'speech': '/audio/speech', 'chat': '/chat/completions'},
}
PROVIDER_KINDS = tuple(PROVIDER_PATHS)

# Request keys the gateway owns; route params can never replace them.
RESERVED_PARAMS = frozenset({'model', 'prompt', 'input', 'messages', 'input_audio', 'file',
                             'stream', 'stream_options'})

AUDIO_FORMATS = ('wav', 'mp3', 'flac', 'm4a', 'ogg', 'webm', 'aac')
MAX_INLINE_AUDIO_BYTES = 256 * 1024
MAX_INPUT_ARTIFACT_BYTES = 25 * 1024 * 1024

MAX_RESPONSE_BYTES = {
    'image': 64 * 1024 * 1024,
    'speech': 32 * 1024 * 1024,
    'transcription': 4 * 1024 * 1024,
    'chat': 2 * 1024 * 1024,
}

MEDIA_EXTENSIONS = {
    'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif',
    'image/svg+xml': 'svg', 'audio/mpeg': 'mp3', 'audio/mp3': 'mp3', 'audio/wav': 'wav',
    'audio/x-wav': 'wav', 'audio/wave': 'wav', 'audio/pcm': 'pcm', 'audio/l16': 'pcm',
    'audio/ogg': 'ogg', 'audio/opus': 'opus', 'audio/aac': 'aac', 'audio/flac': 'flac',
    'audio/webm': 'webm', 'text/plain': 'txt', 'application/json': 'json',
    'application/octet-stream': 'bin',
}
SPEECH_FORMAT_MEDIA = {'mp3': 'audio/mpeg', 'pcm': 'audio/pcm', 'wav': 'audio/wav', 'opus': 'audio/opus',
                       'aac': 'audio/aac', 'flac': 'audio/flac'}
IMAGE_FORMAT_MEDIA = {'png': 'image/png', 'jpeg': 'image/jpeg', 'jpg': 'image/jpeg', 'webp': 'image/webp',
                      'svg': 'image/svg+xml'}

_CONFIRM = {'type': 'boolean', 'description': 'Explicit confirmation after a confirmation_required result.'}


def input_schema(operation):
    """Bounded tool input per operation; binary input is an artifact reference."""
    if operation == 'image':
        properties = {'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
                      'confirm': _CONFIRM}
        required = ['prompt']
    elif operation == 'speech':
        properties = {'text': {'type': 'string', 'minLength': 1, 'maxLength': 4096},
                      'voice': {'type': 'string', 'pattern': '^[A-Za-z0-9_.-]{1,64}$'},
                      'confirm': _CONFIRM}
        required = ['text']
    elif operation == 'transcription':
        properties = {
            'audio_ref': {'type': 'string', 'minLength': 1, 'maxLength': 1024,
                          'description': 'Path of an audio artifact under the gateway artifact_root (relative, or absolute inside it).'},
            'audio_base64': {'type': 'string', 'minLength': 4,
                             'maxLength': 4 * ((MAX_INLINE_AUDIO_BYTES + 2) // 3),
                             'description': 'Small inline audio only (at most 256 KiB decoded); use audio_ref otherwise.'},
            'format': {'type': 'string', 'enum': list(AUDIO_FORMATS)},
            'language': {'type': 'string', 'pattern': '^[a-z]{2}$'},
            'confirm': _CONFIRM}
        required = ['format']
    elif operation == 'chat':
        properties = {'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 32000},
                      'system': {'type': 'string', 'minLength': 1, 'maxLength': 8000},
                      'max_tokens': {'type': 'integer', 'minimum': 1, 'maximum': 16384},
                      'confirm': _CONFIRM}
        required = ['prompt']
    else:
        # A configured capability without a known operation is listed, but every
        # call returns unsupported_modality before any request.
        properties = {'confirm': _CONFIRM}
        required = []
    return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}


def _json(value):
    return leaf.canonical_bytes(value, ascii=True, allow_nan=True)


def _multipart(fields, file_field, filename, file_type, data):
    """Deterministic multipart body so the request digest is reproducible."""
    boundary = 'kpgw-' + leaf.canonical_sha256_with(fields, data, ascii=True, allow_nan=True)[:32]
    parts = []
    for name, value in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                  f'Content-Type: {file_type}\r\n\r\n').encode() + data + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts), 'multipart/form-data; boundary=' + boundary


def _scalar(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, separators=(',', ':'))


def build_request(kind, operation, model, params, arguments, audio=None):
    """Return (path, body, content_type, accept) for one provider request."""
    path = PROVIDER_PATHS[kind][operation]
    params = dict(params or {})
    if operation == 'image':
        body = dict(params, model=model, prompt=arguments['prompt'])
        return path, _json(body), 'application/json', 'application/json'
    if operation == 'speech':
        body = dict(params, model=model, input=arguments['text'])
        if 'voice' in arguments:
            body['voice'] = arguments['voice']
        body.setdefault('response_format', 'mp3')
        return path, _json(body), 'application/json', 'audio/*'
    if operation == 'chat':
        messages = []
        if 'system' in arguments:
            messages.append({'role': 'system', 'content': arguments['system']})
        messages.append({'role': 'user', 'content': arguments['prompt']})
        body = dict(params, model=model, messages=messages, stream=False)
        if 'max_tokens' in arguments:
            body['max_tokens'] = arguments['max_tokens']
        body.setdefault('max_tokens', 1024)
        return path, _json(body), 'application/json', 'application/json'
    if operation == 'transcription':
        fmt = arguments['format']
        if kind == 'openrouter':
            body = dict(params, model=model, input_audio={'data': base64.b64encode(audio).decode(), 'format': fmt})
            if 'language' in arguments:
                body['language'] = arguments['language']
            return path, _json(body), 'application/json', 'application/json'
        fields = [('model', model)]
        merged = dict(params)
        if 'language' in arguments:
            merged['language'] = arguments['language']
        merged.setdefault('response_format', 'json')
        fields += [(key, _scalar(merged[key])) for key in sorted(merged)]
        body, ctype = _multipart(fields, 'file', 'audio.' + fmt, 'audio/' + ('mpeg' if fmt == 'mp3' else fmt), audio)
        return path, body, ctype, 'application/json'
    raise ValueError('unsupported operation')


_USAGE_FIELDS = ('prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens',
                 'output_tokens', 'seconds', 'cost')


def safe_usage(value):
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in _USAGE_FIELDS
            if type(value.get(key)) in (int, float) and math.isfinite(value[key]) and 0 <= value[key] <= 2 ** 53}


def _bounded_label(value):
    return value if isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable() else None


def _sniff_image(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data.startswith(b'GIF8'):
        return 'image/gif'
    head = data[:256].lstrip()
    if head.startswith(b'<svg') or (head.startswith(b'<?xml') and b'<svg' in data[:1024]):
        return 'image/svg+xml'
    return None


def _invalid():
    return ProviderCallFailed('provider_response_invalid', request_sent=True)


def _decode_json(response):
    try:
        value = json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        raise _invalid() from None
    if not isinstance(value, dict) or value.get('error'):
        raise _invalid()
    return value


def parse_response(operation, response, params):
    """Return (outputs, usage, metadata). outputs = [(bytes, media_type)]."""
    params = params or {}
    metadata = {}
    if operation == 'speech':
        media = response.content_type
        if media in (None, 'application/octet-stream'):
            media = SPEECH_FORMAT_MEDIA.get(str(params.get('response_format', 'mp3')))
        if media not in MEDIA_EXTENSIONS or not media.startswith('audio/') or not response.body:
            raise _invalid()
        return [(response.body, media)], {}, metadata
    value = _decode_json(response)
    usage = safe_usage(value.get('usage'))
    for source, target in (('id', 'generation_id'), ('model', 'model_reported')):
        label = _bounded_label(value.get(source))
        if label:
            metadata[target] = label
    if operation == 'image':
        data = value.get('data')
        if not isinstance(data, list) or not 1 <= len(data) <= 10:
            raise _invalid()
        outputs = []
        for item in data:
            if not isinstance(item, dict):
                raise _invalid()
            encoded = item.get('b64_json')
            if not isinstance(encoded, str):
                # URL-only results would need a second, unbudgeted fetch from an
                # arbitrary host; the gateway refuses rather than guess.
                raise ProviderCallFailed('provider_response_unsupported', request_sent=True)
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                raise _invalid() from None
            if not raw:
                raise _invalid()
            declared = item.get('media_type')
            media = (declared if declared in MEDIA_EXTENSIONS and str(declared).startswith('image/')
                     else _sniff_image(raw) or IMAGE_FORMAT_MEDIA.get(str(params.get('output_format')))
                     or 'application/octet-stream')
            outputs.append((raw, media))
        return outputs, usage, metadata
    if operation == 'transcription':
        text = value.get('text')
        if not isinstance(text, str):
            raise _invalid()
        if params.get('response_format') == 'verbose_json':
            return [(_json(value), 'application/json')], usage, metadata
        return [(text.encode(), 'text/plain')], usage, metadata
    if operation == 'chat':
        choices = value.get('choices')
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise _invalid()
        finish = choices[0].get('finish_reason')
        message = choices[0].get('message')
        if not isinstance(message, dict) or not isinstance(message.get('content'), str) or not message['content']:
            raise _invalid()
        if finish not in ('stop', 'length'):
            raise _invalid()
        metadata['finish_reason'] = finish
        return [(message['content'].encode(), 'text/plain')], usage, metadata
    raise ValueError('unsupported operation')
