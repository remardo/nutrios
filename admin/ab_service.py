import logging
import os
from typing import Mapping, Optional

import httpx


logger = logging.getLogger(__name__)


class ABFlagServiceError(RuntimeError):
    """Raised when the AB flag service fails to apply a configuration."""


class ABFlagService:
    """Simple client for pushing experiment configurations to the AB flag service."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 5.0,
    ) -> None:
        self.base_url = base_url or os.getenv("ABFLAG_BASE_URL")
        self.api_key = api_key or os.getenv("ABFLAG_API_KEY")
        self.timeout = timeout

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post(self, path: str, payload: dict) -> None:
        if not self.base_url:
            logger.debug("ABFlagService base URL not configured; skipping request to %s", path)
            return
        url = f"{self.base_url.rstrip('/')}{path}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(url, json=payload, headers=self._build_headers())
                response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("ABFlagService request to %s failed: %s", url, exc)
            raise ABFlagServiceError(str(exc)) from exc

    def publish_experiment(
        self,
        experiment_key: str,
        rollout_percentage: float,
        variant_weights: Mapping[str, float],
        preserve_sticky_assignments: bool = True,
    ) -> None:
        payload = {
            "rollout_percentage": rollout_percentage,
            "variant_weights": dict(variant_weights),
            "preserve_sticky_assignments": preserve_sticky_assignments,
        }
        self._post(f"/experiments/{experiment_key}/publish", payload)

    def pause_experiment(self, experiment_key: str) -> None:
        self._post(f"/experiments/{experiment_key}/pause", {"preserve_sticky_assignments": True})

    def resume_experiment(
        self,
        experiment_key: str,
        rollout_percentage: float,
        variant_weights: Mapping[str, float],
    ) -> None:
        # Resuming is equivalent to publishing the latest configuration.
        self.publish_experiment(
            experiment_key,
            rollout_percentage,
            variant_weights,
            preserve_sticky_assignments=True,
        )


def get_ab_service() -> ABFlagService:
    """Factory used by FastAPI dependency injection."""

    return ABFlagService()

