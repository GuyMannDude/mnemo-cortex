"""
Mem0 upstream bridge for Mnemo Cortex.
Optional fallback tier — queries Mem0 cloud when local search misses.
"""

import logging
import time
from typing import Optional
import httpx

from .config import Mem0Config
from .cache import ContextChunk

log = logging.getLogger("agentb.mem0")


class Mem0Bridge:
    """Thin async client for the Mem0 REST API."""

    def __init__(self, config: Mem0Config):
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.api_base,
            headers={"Authorization": f"Token {config.api_key}",
                     "Content-Type": "application/json"},
            timeout=config.timeout,
        )
        self._call_count = 0
        log.info(f"Mem0 bridge initialized (base={config.api_base}, "
                 f"user_id={config.user_id or 'per-agent'})")

    async def search(self, query: str, user_id: Optional[str] = None,
                     top_k: int = 3) -> list[ContextChunk]:
        """Search Mem0 for memories matching the query."""
        uid = user_id or self.config.user_id
        if not uid:
            log.warning("Mem0 search skipped: no user_id configured")
            return []

        try:
            self._call_count += 1
            start = time.time()
            resp = await self._client.post("/memories/search/", json={
                "query": query,
                "user_id": uid,
                "limit": top_k,
            })
            elapsed = (time.time() - start) * 1000

            if resp.status_code != 200:
                log.warning(f"Mem0 search failed: HTTP {resp.status_code} "
                            f"({resp.text[:200]})")
                return []

            data = resp.json()
            results = data if isinstance(data, list) else data.get("results", data.get("memories", []))
            chunks = []
            for item in results[:top_k]:
                memory_text = item.get("memory", item.get("content", item.get("text", "")))
                score = item.get("score", item.get("relevance", 0.5))

                if not memory_text:
                    continue
                if score < self.config.min_relevance:
                    continue

                chunks.append(ContextChunk(
                    content=memory_text,
                    source=f"mem0:{item.get('id', 'unknown')}",
                    relevance=round(float(score), 4),
                    cache_tier="MEM0",
                ))

            log.info(f"Mem0 search: {len(chunks)} results in {elapsed:.0f}ms "
                     f"(call #{self._call_count}, user={uid})")
            return chunks

        except httpx.TimeoutException:
            log.warning(f"Mem0 search timed out after {self.config.timeout}s")
            return []
        except Exception as e:
            log.warning(f"Mem0 search error: {e}")
            return []

    async def add(self, messages: list[dict], user_id: Optional[str] = None,
                  metadata: Optional[dict] = None) -> Optional[str]:
        """Push a memory to Mem0."""
        uid = user_id or self.config.user_id
        if not uid:
            log.warning("Mem0 add skipped: no user_id configured")
            return None

        try:
            self._call_count += 1
            body = {"messages": messages, "user_id": uid}
            if metadata:
                body["metadata"] = metadata

            resp = await self._client.post("/memories/", json=body)

            if resp.status_code not in (200, 201):
                log.warning(f"Mem0 add failed: HTTP {resp.status_code} "
                            f"({resp.text[:200]})")
                return None

            data = resp.json()
            if isinstance(data, list):
                mem_id = data[0].get("id", "ok") if data else "ok"
            elif isinstance(data, dict):
                mem_id = data.get("id", data.get("memory_id", "ok"))
            else:
                mem_id = "ok" 
            log.info(f"Mem0 add: saved as {mem_id} (user={uid})")
            return str(mem_id)

        except httpx.TimeoutException:
            log.warning(f"Mem0 add timed out after {self.config.timeout}s")
            return None
        except Exception as e:
            log.warning(f"Mem0 add error: {e}")
            return None

    @property
    def call_count(self) -> int:
        return self._call_count

    async def close(self):
        await self._client.aclose()
