"""Integration smoke test for a fresh, local PostgreSQL database only."""
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ["api-gateway", "user-service", "restaurant-service", "order-service", "payment-service", "notification-service"]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    url = os.environ.get("DATABASE_URL", "")
    if urlparse(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("Set DATABASE_URL to a disposable local PostgreSQL database")
    with psycopg.connect(url) as connection:
        encoding = connection.execute("SHOW server_encoding").fetchone()[0]
        if encoding not in {"UTF8", b"UTF8"}:
            raise RuntimeError("Use a PostgreSQL database created with UTF8 encoding")
        if connection.execute("SELECT to_regclass('public.users')").fetchone()[0] is not None:
            raise RuntimeError("Use a fresh database; this test initializes schema and seed data")
        for file in ("schema.sql", "seed.sql"):
            connection.execute((ROOT / "database" / file).read_text())
    ports = {service: free_port() for service in SERVICES}
    env = dict(os.environ, JWT_SECRET="smoke-" + uuid.uuid4().hex)
    for service in SERVICES[1:]:
        env["UPSTREAM_" + service.split("-")[0].upper()] = f"http://127.0.0.1:{ports[service]}"
    processes = []
    try:
        for service in SERVICES:
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(ports[service]), "--ws", "websockets-sansio"],
                cwd=ROOT / "app" / service, env=env,
            ))
        for service in SERVICES:
            for attempt in range(100):
                try:
                    if httpx.get(f"http://127.0.0.1:{ports[service]}/ready", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError(f"{service} did not become ready")
        with httpx.Client(base_url=f"http://127.0.0.1:{ports['api-gateway']}", timeout=15) as client:
            def call(method, path, **kwargs):
                response = client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json()
            account = {"email": f"smoke-{uuid.uuid4().hex}@example.com", "password": "LocalTestPassword123!"}
            call("POST", "/users/register", json=account)
            client.headers["Authorization"] = "Bearer " + call("POST", "/auth/login", json=account)["access_token"]
            assert call("GET", "/me")["email"] == account["email"]
            restaurant = call("GET", "/restaurants")[0]
            menu = call("GET", f"/restaurants/{restaurant['id']}")["menu"][0]
            order = call("POST", "/orders", json={"restaurant_id": restaurant["id"], "items": [{"menu_item_id": menu["id"], "quantity": 1}]})
            assert call("GET", f"/orders/{order['id']}")["items"][0]["quantity"] == 1
            result = call("POST", "/payments", json={"order_id": order["id"], "amount": float(order["total_amount"])})
            assert result["status"] == "SUCCESS" and result["method"] == "MOCK"
            assert call("POST", "/notifications", json={"order_id": order["id"], "message": "Local smoke test"})["status"] == "SENT"
            assert call("GET", "/orders")[0]["status"] == "PAID"
            print("PASS: six running services, authentication, restaurants, orders, mock payments, notifications")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            process.wait(timeout=10)


if __name__ == "__main__":
    main()
