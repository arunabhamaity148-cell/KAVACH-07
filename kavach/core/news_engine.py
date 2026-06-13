"""
KAVACH-07 — News Engine
Fetches, parses, and analyzes crypto news sentiment using OpenAI GPT-4o-mini.
Implements multi-source RSS fallback and automated risk-off safeguards.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger("kavach.news_engine")

@dataclass(slots=True)
class NewsItem:
    """Represents a single news headline and its metadata."""
    headline: str
    source: str
    url: str
    published_at: float  # Unix timestamp
    score: float = 0.0
    impact: str = "LOW"
    processed: bool = False

class NewsEngine:
    """
    Asynchronous news aggregator with AI-driven sentiment analysis.
    Safeguard: If analysis fails for 30+ minutes, triggers risk-off state.
    """

    def __init__(self, config: dict, risk_manager: Any, openai_api_key: str):
        self._cfg = config
        self._risk = risk_manager
        self._openai_key = openai_api_key
        
        # Config Extraction
        n_cfg = config["phase2"]["news"]
        self._poll_interval = int(n_cfg.get("poll_interval_seconds", 120))
        self._max_age_sec = float(n_cfg.get("max_headline_age_hours", 4.0)) * 3600
        self._emergency_thresh = float(n_cfg.get("emergency_score_threshold", -7.0))
        self._rss_feeds = n_cfg.get("rss_feeds", [])
        self._panic_key = n_cfg.get("crypto_panic_api_key", "")
        
        # Internal State
        self._alerts: Optional[Any] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        
        self._seen_urls: set[str] = set()
        self._current_score: float = 0.0
        self._current_impact: str = "LOW"
        self._dominant_source: str = "NONE"
        
        self._last_successful_analysis: float = time.time()
        self._is_degraded: bool = False
        
        # Keyword Fallback Dictionary (Bug #8)
        self._neg_keywords = {"hack", "exploit", "ban", "scam", "lawsuit", "sec", "theft", "insolvent", "bankrupt"}
        self._pos_keywords = {"approval", "etf", "partnership", "adoption", "launch", "upgrade", "bullish"}

    def set_alert_manager(self, alert_manager: Any) -> None:
        """Injects alert manager to resolve circular dependency (Bug #16)."""
        self._alerts = alert_manager

    async def start(self) -> None:
        """Starts the news polling loop."""
        if not self._openai_key:
            logger.error("News Engine: OPENAI_API_KEY missing. Engine will run in keyword-only mode.")
            self._is_degraded = True

        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self._running = True
        asyncio.create_task(self._poll_loop())
        logger.info("News Engine started. Multi-source RSS fallback active.")

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        if self._session:
            await self._session.close()
        logger.info("News Engine stopped")

    def get_status(self) -> Dict[str, Any]:
        """Returns the current market sentiment snapshot."""
        # Risk-off trigger if data is stale (Bug #8)
        if time.time() - self._last_successful_analysis > 1800:
            return {"score": -3.0, "impact": "HIGH", "dominant_source": "STALE_DATA"}
            
        return {
            "score": self._current_score,
            "impact": self._current_impact,
            "dominant_source": self._dominant_source
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Polling & Processing
    # ──────────────────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Main orchestrator for news ingestion."""
        while self._running:
            try:
                headlines = await self._fetch_all_sources()
                new_headlines = [h for h in headlines if h.url not in self._seen_urls]
                
                if new_headlines:
                    await self._analyze_headlines(new_headlines)
                    for h in new_headlines:
                        self._seen_urls.add(h.url)
                    
                    self._aggregate_sentiment(new_headlines)
                    self._last_successful_analysis = time.time()
                    
                    # Memory cleanup
                    if len(self._seen_urls) > 5000:
                        self._seen_urls = set(list(self._seen_urls)[-2000:])
                
                # Check for emergency threshold
                if self._current_score <= self._emergency_thresh and self._current_impact == "HIGH":
                    await self._trigger_emergency()

            except Exception as e:
                logger.error("News Engine poll loop error: %s", e)
            
            await asyncio.sleep(self._poll_interval)

    async def _fetch_all_sources(self) -> List[NewsItem]:
        """Aggregates headlines from Panic and backup RSS feeds."""
        tasks = [self._fetch_rss(url) for url in self._rss_feeds]
        if self._panic_key:
            tasks.append(self._fetch_cryptopanic())
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        flat_list = []
        now = time.time()
        for res in results:
            if isinstance(res, list):
                # Filter by age (Bug #7)
                valid = [h for h in res if (now - h.published_at) <= self._max_age_sec]
                flat_list.extend(valid)
        return flat_list

    async def _fetch_rss(self, url: str) -> List[NewsItem]:
        """Parses standard RSS feeds async."""
        try:
            async with self._session.get(url) as resp:
                if resp.status != 200: return []
                content = await resp.text()
                tree = ET.fromstring(content)
                items = []
                for entry in tree.findall(".//item"):
                    title = entry.findtext("title")
                    link = entry.findtext("link")
                    pub_date = entry.findtext("pubDate")
                    
                    if title and link and pub_date:
                        try:
                            dt = parsedate_to_datetime(pub_date)
                            ts = dt.timestamp()
                            items.append(NewsItem(headline=title, source=url, url=link, published_at=ts))
                        except Exception:
                            continue
                return items
        except Exception as e:
            logger.warning("RSS Fetch failed for %s: %s", url, e)
            return []

    async def _fetch_cryptopanic(self) -> List[NewsItem]:
        """Fetches from CryptoPanic API."""
        url = "https://cryptopanic.com/api/v1/posts/"
        params = {"auth_token": self._panic_key, "public": "true"}
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200: return []
                data = await resp.json()
                items = []
                for p in data.get("results", []):
                    title = p.get("title")
                    link = p.get("url")
                    p_date = p.get("published_at")
                    if title and link and p_date:
                        # CryptoPanic uses ISO format
                        dt = datetime.fromisoformat(p_date.replace("Z", "+00:00"))
                        items.append(NewsItem(headline=title, source="CryptoPanic", url=link, published_at=dt.timestamp()))
                return items
        except Exception as e:
            logger.warning("CryptoPanic Fetch failed: %s", e)
            return []

    # ──────────────────────────────────────────────────────────────────────────
    # Analysis Logic
    # ──────────────────────────────────────────────────────────────────────────

    async def _analyze_headlines(self, items: List[NewsItem]) -> None:
        """Analyzes a batch of headlines using OpenAI or Keyword Fallback."""
        for item in items:
            if not self._openai_key:
                self._apply_keyword_fallback(item)
                continue
                
            try:
                await self._openai_classify(item)
            except Exception as e:
                logger.error("OpenAI Analysis failed for '%s': %s", item.headline, e)
                self._apply_keyword_fallback(item)

    async def _openai_classify(self, item: NewsItem) -> None:
        """Sends headline to GPT-4o-mini for structured JSON scoring."""
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self._openai_key}", "Content-Type": "application/json"}
        
        prompt = (
            "Analyze this crypto news headline and respond in JSON format ONLY: "
            f"'{item.headline}'. Score: float -10 to +10. Impact: LOW, MEDIUM, or HIGH. "
            "JSON structure: {\"score\": float, \"impact\": \"string\"}"
        )
        
        payload = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.0
        }
        
        async with self._session.post(url, headers=headers, json=payload) as resp:
            if resp.status == 200:
                res = await resp.json()
                content = json.loads(res["choices"][0]["message"]["content"])
                item.score = float(content.get("score", 0.0))
                item.impact = content.get("impact", "LOW").upper()
                item.processed = True
            else:
                raise Exception(f"OpenAI Status {resp.status}")

    def _apply_keyword_fallback(self, item: NewsItem) -> None:
        """Heuristic scoring based on keyword matching."""
        lower_hl = item.headline.lower()
        score = 0.0
        impact = "LOW"
        
        for kw in self._neg_keywords:
            if kw in lower_hl:
                score -= 4.0
                impact = "MEDIUM"
        
        for kw in self._pos_keywords:
            if kw in lower_hl:
                score += 3.0
                impact = "MEDIUM"
                
        item.score = score
        item.impact = impact
        item.processed = True

    def _aggregate_sentiment(self, items: List[NewsItem]) -> None:
        """Updates global sentiment state based on latest batch."""
        if not items: return
        
        avg_score = sum(h.score for h in items) / len(items)
        # We take the highest impact from the batch
        impacts = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        max_impact = max(items, key=lambda x: impacts.get(x.impact, 0)).impact
        
        # Weighted Smoothing (EMA-like)
        alpha = 0.3
        self._current_score = (alpha * avg_score) + ((1 - alpha) * self._current_score)
        self._current_impact = max_impact
        self._dominant_source = items[0].source

    async def _trigger_emergency(self) -> None:
        """Automated risk-off trigger for severe news events."""
        logger.critical("NEWS EMERGENCY: Sentiment %.1f | Impact HIGH", self._current_score)
        if self._alerts:
            await self._alerts.send_text(
                f"🚨 *NEWS EMERGENCY TRIGGERED*\n"
                f"Sentiment Score: `{self._current_score:.1f}`\n"
                f"Impact: `HIGH`\n"
                f"Bot will pause for 60 minutes."
            )
        self._risk.pause(60)