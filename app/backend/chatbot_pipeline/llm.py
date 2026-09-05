"""
chatbot/llm.py - Minimal LLM client for the chatbot.

Talks to a local Ollama instance by default (CHATBOT_LLM_PROVIDER / OLLAMA_HOST
/ OLLAMA_MODEL in .env), or to an OpenAI-compatible chat-completions API when
CHATBOT_LLM_PROVIDER is "openrouter" (OPENROUTER_API_KEY / OPENROUTER_MODEL) or
"groq" (GROQ_API_KEY / GROQ_MODEL) - both providers speak the same request/
response shape, just different base URLs and keys. Kept deliberately thin: one
`complete()` call, an optional JSON mode, and an `available()` probe so every
stage can degrade to its deterministic fallback instead of raising when no
model is reachable.

That fallback path is the reason this is a class and not a bare requests call -
the classifier has heuristics, text-to-Cypher has template queries, and GraphRAG
can still return retrieved logs verbatim. A missing LLM should soften the
answers, not break the chatbot.
"""

import json
import logging
import re
import time
from typing import Any, Dict, Optional

from config import LLM_CONFIG

logger = logging.getLogger(__name__)

# How long an "unreachable" verdict is trusted before re-probing. A one-shot
# CLI call never lives long enough for this to matter, but a chatbot API
# server does - without a TTL, starting the backend before Ollama (or Ollama
# restarting) would wedge every request into the deterministic fallback until
# the whole server process was restarted.
_AVAILABILITY_TTL_SECONDS = 15

# How many times a 429 is retried before giving up. The hosted free tiers
# refill per minute, so a couple of short waits clears the common case; more
# than that and the caller is better served by a fast, honest failure.
_MAX_RATE_LIMIT_RETRIES = 3
_DEFAULT_RETRY_DELAY = 2.0
_MAX_RETRY_DELAY = 20.0
# Providers put the wait either in a Retry-After header or in the error prose
# ("Please try again in 1.5675s"). Both are honoured; the header wins.
_RETRY_IN_RE = re.compile(r'try again in ([0-9.]+)\s*(ms|s)\b', re.IGNORECASE)


def _retry_delay_seconds(response) -> float:
    header = response.headers.get('retry-after')
    if header:
        try:
            return min(max(float(header), 0.1), _MAX_RETRY_DELAY)
        except ValueError:
            pass
    match = _RETRY_IN_RE.search(response.text or '')
    if match:
        value = float(match.group(1))
        if match.group(2).lower() == 'ms':
            value /= 1000.0
        # A small cushion: the stated delay is when the window *starts* to
        # clear, and retrying at exactly that instant tends to 429 again.
        return min(max(value + 0.35, 0.2), _MAX_RETRY_DELAY)
    return _DEFAULT_RETRY_DELAY

# provider -> (base URL, api-key config field, model config field)
_OPENAI_COMPATIBLE_PROVIDERS = {
    'openrouter': ("https://openrouter.ai/api/v1", 'openrouter_api_key', 'openrouter_model'),
    'groq': ("https://api.groq.com/openai/v1", 'groq_api_key', 'groq_model'),
}


# Qwen3 ignores both `think: false` and `/no_think`, and streams its reasoning
# into the normal response terminated by a bare `</think>` WITH NO OPENING TAG -
# so neither the provider's `thinking` field nor a `<think>`-anchored regex
# catches it. Everything before the last closing tag is reasoning; if the budget
# ran out there is no closing tag at all, which is why constrained decoding is
# the real fix and this is the backstop.
_THINK_END = '</think>'


def _strip_reasoning(text: str) -> str:
    if not text:
        return ''
    if _THINK_END in text:
        text = text.rsplit(_THINK_END, 1)[1]
    return text.strip()


class LLMClient:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or LLM_CONFIG
        self.provider = (cfg.get('provider') or 'ollama').strip().lower()
        self.host = cfg.get('ollama_host', 'http://localhost:11434').rstrip('/')
        self.openrouter_api_key = cfg.get('openrouter_api_key', '')
        self.openrouter_model = cfg.get('openrouter_model', 'openai/gpt-4o-mini')
        self.groq_api_key = cfg.get('groq_api_key', '')
        self.groq_model = cfg.get('groq_model', 'llama-3.3-70b-versatile')
        # .model is what callers (e.g. api.py's /api/health) display - whichever
        # provider is active, this should name the model actually being used.
        if self.provider in _OPENAI_COMPATIBLE_PROVIDERS:
            _, _, model_field = _OPENAI_COMPATIBLE_PROVIDERS[self.provider]
            self.model = getattr(self, model_field)
        else:
            self.model = cfg.get('ollama_model', 'qwen3:14b')
        self.timeout = cfg.get('timeout', 120)
        self._available: Optional[bool] = None
        self._checked_at: float = 0.0
        # Why the last completion failed, for callers that want to say
        # something truer than "no LLM available".
        self.last_error: Optional[str] = None

    # -- runtime provider switching -----------------------------------------
    def providers(self) -> Dict[str, Dict[str, Any]]:
        """What this process could switch to, and whether each is usable.

        `configured` means the credentials/host are present - not that the
        provider answered. Deliberately does NOT probe every provider on every
        call: a probe costs a network round-trip each, and the UI asks for this
        on a timer.
        """
        return {
            'ollama': {
                'label': 'Ollama (local / airgapped)',
                'model': LLM_CONFIG.get('ollama_model', 'qwen3:14b'),
                'target': self.host,
                'configured': True,   # a local host is always "set"; reachability is separate
                'needs_network': False,
            },
            'groq': {
                'label': 'Groq (cloud API)',
                'model': self.groq_model,
                'target': _OPENAI_COMPATIBLE_PROVIDERS['groq'][0],
                'configured': bool(self.groq_api_key and not self.groq_api_key.startswith('your_')),
                'needs_network': True,
            },
            'openrouter': {
                'label': 'OpenRouter (cloud API)',
                'model': self.openrouter_model,
                'target': _OPENAI_COMPATIBLE_PROVIDERS['openrouter'][0],
                'configured': bool(self.openrouter_api_key and not self.openrouter_api_key.startswith('your_')),
                'needs_network': True,
            },
        }

    def switch_provider(self, provider: str) -> None:
        """Repoints this client at a different provider, in place.

        In place, rather than returning a new client, precisely because
        LogChatbot hands this same object to TextToCypher, GraphRAG and the
        Planner at construction time - they all hold the same reference, so
        mutating it switches every one of them at once. Returning a new
        instance would leave those three still talking to the old provider,
        which is the kind of half-applied setting that is very hard to
        diagnose from the outside.
        """
        provider = (provider or '').strip().lower()
        if provider not in self.providers():
            raise ValueError(f"Unknown provider {provider!r}. Valid: {sorted(self.providers())}")
        self.provider = provider
        if provider in _OPENAI_COMPATIBLE_PROVIDERS:
            _, _, model_field = _OPENAI_COMPATIBLE_PROVIDERS[provider]
            self.model = getattr(self, model_field)
        else:
            self.model = LLM_CONFIG.get('ollama_model', 'qwen3:14b')
        # The cached verdict belongs to the OLD provider - carrying it over
        # would report the new one as unreachable (or reachable) without ever
        # having contacted it.
        self._available = None
        self._checked_at = 0.0
        logger.info(f"LLM provider switched to {provider} (model {self.model})")

    def available(self) -> bool:
        """Reachability probe, cached briefly. A positive result is trusted
        for the TTL (avoids a round-trip on every single call); a negative
        result is retried after the TTL rather than trusted forever, so the
        chatbot recovers on its own once Ollama comes up."""
        now = time.monotonic()
        if self._available is not None and (now - self._checked_at) < _AVAILABILITY_TTL_SECONDS:
            return self._available
        try:
            import requests
            if self.provider in _OPENAI_COMPATIBLE_PROVIDERS:
                base, key_field, _ = _OPENAI_COMPATIBLE_PROVIDERS[self.provider]
                api_key = getattr(self, key_field)
                if not api_key or api_key.startswith('your_'):
                    logger.info(f"{key_field.upper()} not set; using deterministic fallbacks.")
                    self._available = False
                else:
                    # /models is free (no completion tokens spent). It is also
                    # checked against the CONFIGURED MODEL, not just for a 200:
                    # these providers retire hosted models regularly, so a
                    # perfectly valid key happily returns 200 here while every
                    # actual completion 404s with model_not_found. Treating
                    # "key works" as "provider usable" made the UI show a green
                    # dot for a provider that could not answer a single
                    # question.
                    response = requests.get(
                        f"{base}/models",
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=5,
                    )
                    if response.status_code != 200:
                        self._available = False
                    else:
                        model = getattr(self, _OPENAI_COMPATIBLE_PROVIDERS[self.provider][2])
                        ids = {m.get('id') for m in (response.json().get('data') or [])}
                        # An empty/unparseable list means the provider doesn't
                        # enumerate models - fall back to trusting the key
                        # rather than declaring a working provider unusable.
                        self._available = (model in ids) if ids else True
                        if ids and model not in ids:
                            logger.warning(
                                f"{self.provider} key is valid but model {model!r} is not available "
                                f"to it. Set {self.provider.upper()}_MODEL to one of: "
                                f"{', '.join(sorted(ids)[:8])}"
                            )
            else:
                response = requests.get(f"{self.host}/api/tags", timeout=5)
                self._available = response.status_code == 200
        except Exception as e:
            logger.info(f"LLM not reachable ({self.provider}) ({e}); using deterministic fallbacks.")
            self._available = False
        self._checked_at = now
        return self._available

    def complete(self, prompt: str, system: Optional[str] = None,
                 json_mode: bool = False, temperature: float = 0.0) -> Optional[str]:
        """Returns the model's text, or None if the model isn't usable."""
        if not self.available():
            return None
        if self.provider in _OPENAI_COMPATIBLE_PROVIDERS:
            return self._complete_openai_compatible(prompt, system, json_mode, temperature)
        return self._complete_ollama(prompt, system, json_mode, temperature)

    def _complete_ollama(self, prompt: str, system: Optional[str],
                          json_mode: bool, temperature: float) -> Optional[str]:
        try:
            import requests
            payload: Dict[str, Any] = {
                'model': self.model,
                'prompt': prompt,
                'stream': False,
                # Deterministic by default: the same question should produce the
                # same Cypher, or the chatbot is impossible to debug.
                'options': {'temperature': temperature},
            }
            if system:
                payload['system'] = system
            if json_mode:
                payload['format'] = 'json'

            response = requests.post(
                f"{self.host}/api/generate", json=payload, timeout=self.timeout
            )
            if response.status_code != 200:
                logger.warning(f"LLM returned HTTP {response.status_code}")
                return None
            return _strip_reasoning(response.json().get('response', ''))
        except Exception as e:
            logger.warning(f"LLM call failed: {e}")
            return None

    def _complete_openai_compatible(self, prompt: str, system: Optional[str],
                                     json_mode: bool, temperature: float) -> Optional[str]:
        try:
            import requests
            base, key_field, model_field = _OPENAI_COMPATIBLE_PROVIDERS[self.provider]
            api_key = getattr(self, key_field)
            model = getattr(self, model_field)

            messages = []
            if system:
                messages.append({'role': 'system', 'content': system})
            messages.append({'role': 'user', 'content': prompt})
            payload: Dict[str, Any] = {
                'model': model,
                'messages': messages,
                'temperature': temperature,
            }
            if json_mode:
                # Not every model behind these providers supports structured
                # output; if the provider rejects this, the request fails and
                # complete() returns None like any other unusable-model case -
                # the caller's deterministic fallback takes over, same as a
                # timeout or an unreachable host would.
                payload['response_format'] = {'type': 'json_object'}

            # Retry on 429. The hosted tiers are rate-limited per minute
            # (Groq's free tier is 8k tokens/min), and a single GraphRAG
            # answer - a 12k-char evidence prompt plus the reply - can trip it
            # on its own. Without this the call just returned None and every
            # stage fell back to raw rows, which looks exactly like "the
            # chatbot is broken" while the provider is merely asking us to
            # wait about a second. These APIs tell us how long to wait, so
            # waiting is the correct behaviour, not an optimisation.
            for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
                response = requests.post(
                    f"{base}/chat/completions",
                    headers={
                        'Authorization': f"Bearer {api_key}",
                        'Content-Type': 'application/json',
                    },
                    json=payload,
                    timeout=self.timeout,
                )
                if response.status_code == 429 and attempt < _MAX_RATE_LIMIT_RETRIES:
                    delay = _retry_delay_seconds(response)
                    logger.info(
                        f"{self.provider} rate-limited (429); retrying in {delay:.1f}s "
                        f"(attempt {attempt + 1}/{_MAX_RATE_LIMIT_RETRIES})"
                    )
                    time.sleep(delay)
                    continue
                break

            if response.status_code != 200:
                detail = response.text[:300]
                self.last_error = (
                    f"rate limited by {self.provider} - too many tokens per minute"
                    if response.status_code == 429
                    else f"{self.provider} returned HTTP {response.status_code}"
                )
                logger.warning(f"LLM returned HTTP {response.status_code}: {detail}")
                return None
            choices = response.json().get('choices') or []
            if not choices:
                self.last_error = f"{self.provider} returned no completion"
                return None
            self.last_error = None
            return (choices[0].get('message', {}).get('content') or '').strip()
        except Exception as e:
            self.last_error = f"{self.provider} call failed: {e}"
            logger.warning(f"LLM call failed: {e}")
            return None

    def complete_json(self, prompt: str, system: Optional[str] = None) -> Optional[Dict[str, Any]]:
        raw = self.complete(prompt, system=system, json_mode=True)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.debug(f"LLM returned non-JSON despite json mode: {raw[:200]}")
            return None
