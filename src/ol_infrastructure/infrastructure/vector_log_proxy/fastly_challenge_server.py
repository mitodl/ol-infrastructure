"""
Simple HTTP server that returns Fastly service ID hashes for RFC 8615 challenge.

This server queries the Fastly API to get all service IDs, computes their
SHA-256 hashes, and returns them as a newline-delimited text response for
Fastly's domain ownership verification.

Reference:
https://www.fastly.com/documentation/guides/integrations/logging-endpoints/log-streaming-https/
"""

import hashlib
import logging
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FASTLY_API_KEY = os.environ.get("FASTLY_API_KEY", "")
FASTLY_API_BASE = "https://api.fastly.com"


def get_fastly_service_ids() -> list[str]:
    """Fetch all Fastly service IDs using the Fastly API."""
    if not FASTLY_API_KEY:
        logger.error("FASTLY_API_KEY environment variable not set")
        return []

    headers = {"Fastly-Key": FASTLY_API_KEY, "Accept": "application/json"}

    try:
        response = requests.get(
            f"{FASTLY_API_BASE}/service", headers=headers, timeout=10
        )
        response.raise_for_status()
        services = response.json()

        service_ids = [service["id"] for service in services if "id" in service]
        logger.info("Retrieved %d service IDs from Fastly API", len(service_ids))
    except requests.RequestException:
        logger.exception("Failed to fetch Fastly services")
        return []
    else:
        return service_ids


def compute_service_hashes(service_ids: list[str]) -> str:
    """Compute SHA-256 hashes for service IDs and return as newline-delimited text."""
    hashes = []
    for service_id in service_ids:
        sha256_hash = hashlib.sha256(service_id.encode()).hexdigest()
        hashes.append(sha256_hash)
        logger.debug("Service ID %s -> hash %s", service_id, sha256_hash)

    # Add wildcard to allow all services (per Fastly documentation)
    hashes.append("*")

    return "\n".join(hashes)


class FastlyChallengeHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Fastly RFC 8615 challenge endpoint."""

    def do_GET(self):
        """Handle GET requests to /.well-known/fastly/logging/challenge."""
        if self.path == "/.well-known/fastly/logging/challenge":
            service_ids = get_fastly_service_ids()
            response_body = compute_service_hashes(service_ids)

            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body.encode())
            logger.info(
                "Served challenge with %d service hashes to %s",
                len(service_ids),
                self.client_address[0],
            )
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, format, *args):  # noqa: A002
        """Override to use custom logger."""
        logger.info("%s - %s", self.client_address[0], format % args)


def run_server(port: int = 8080):
    """Run the HTTP server."""
    server_address = ("", port)
    httpd = HTTPServer(server_address, FastlyChallengeHandler)
    logger.info("Starting Fastly challenge server on port %d", port)
    httpd.serve_forever()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    run_server(port)
