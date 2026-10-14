"""Google Cloud Secret Manager integration with local environment variable fallback."""

import logging
import os
from typing import Optional

logger = logging.getLogger("enterprise_support_agent.secrets")


class SecretManagerClient:
    """Resolves credentials from Google Cloud Secret Manager with local environment fallback."""

    @classmethod
    def get_secret(
        cls,
        secret_id: str,
        project_id: Optional[str] = None,
        fallback_env: Optional[str] = None,
    ) -> Optional[str]:
        """Fetch a secret payload from Google Cloud Secret Manager or fallback to an env var.

        Args:
            secret_id: Identifier of the secret in Google Cloud Secret Manager.
            project_id: Optional GCP project ID (defaults to GOOGLE_CLOUD_PROJECT).
            fallback_env: Environment variable name to check first or use as fallback.

        Returns:
            Resolved secret string value if available, otherwise None.
        """
        if fallback_env:
            env_value = os.getenv(fallback_env)
            if env_value and env_value != "your_gemini_api_key_here":
                return env_value

        direct_env = os.getenv(secret_id)
        if direct_env and direct_env != "your_gemini_api_key_here":
            return direct_env

        try:
            from google.cloud import secretmanager  # type: ignore

            gcp_project = project_id or os.getenv("GOOGLE_CLOUD_PROJECT")
            if not gcp_project or gcp_project == "your-gcp-project-id":
                return None

            client = secretmanager.SecretManagerServiceClient()
            secret_path = f"projects/{gcp_project}/secrets/{secret_id}/versions/latest"
            response = client.access_secret_version(request={"name": secret_path})
            secret_payload = response.payload.data.decode("utf-8").strip()
            logger.info("[SecretManager] Retrieved secret '%s' from GCP.", secret_id)
            return secret_payload
        except Exception as exc:
            logger.debug(
                "[SecretManager] GCP Secret Manager unavailable (%s); using env fallback.",
                exc,
            )
            return None


secret_client = SecretManagerClient()
