try:
    from src.core.collaboration.experimental_boundary import ManagedTaskRefusal
except ImportError:
    from core.collaboration.experimental_boundary import ManagedTaskRefusal

import os
import uuid
import sqlite3
import time
import json
from aiohttp import web
from typing import Optional
from cryptography.hazmat.primitives.asymmetric import ed25519


class DashboardServer:
    def __init__(self, mesh, port: int = 8000, frontend_path: Optional[str] = None):
        self.mesh = mesh
        self.port = port
        if frontend_path is None:
            # Default to dashboard_ui/dist relative to this file
            current_dir = os.path.dirname(os.path.abspath(__file__))
            self.frontend_path = os.path.join(current_dir, "dashboard_ui", "dist")
        else:
            self.frontend_path = frontend_path

        # Auth state
        self.challenges = {}  # pub_key -> nonce
        self.sessions = {}  # session_id -> pub_key
        self.invite_code = self._generate_invite_code()
        self._init_auth_db()

        self.app = web.Application(middlewares=[self.auth_middleware])
        self._setup_routes()
        self.runner = None
        self.site = None

    def _init_auth_db(self):
        self.conn = sqlite3.connect(self.mesh.db_path)
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS authorized_identities (
                public_key TEXT PRIMARY KEY,
                created_at REAL
            )
        """)
        self.conn.commit()

    def _generate_invite_code(self):
        code = str(uuid.uuid4())
        print("\n" + "=" * 40)
        print(f"ADMIN INVITE CODE: {code}")
        print("=" * 40 + "\n")
        return code

    def _setup_routes(self):
        # API Routes
        self.app.router.add_post("/api/auth/challenge", self.handle_auth_challenge)
        self.app.router.add_post("/api/auth/verify", self.handle_auth_verify)
        self.app.router.add_post("/api/auth/register", self.handle_auth_register)
        self.app.router.add_post("/api/logout", self.handle_logout)
        self.app.router.add_get("/api/status", self.handle_status)
        self.app.router.add_get("/api/thoughts", self.handle_thoughts)
        self.app.router.add_post("/api/thoughts", self.handle_post_thought)
        self.app.router.add_get("/api/batons", self.handle_batons)
        self.app.router.add_post("/api/batons", self.handle_post_baton)
        self.app.router.add_get("/api/kv", self.handle_kv)
        self.app.router.add_get("/api/backlog", self.handle_backlog)
        self.app.router.add_post("/api/backlog", self.handle_post_backlog)
        self.app.router.add_get("/api/logs", self.handle_logs)
        self.app.router.add_get("/api/network", self.handle_network)
        self.app.router.add_get("/api/discussions", self.handle_discussions)

        # CORS (Simplified for local dev)
        # self.app.on_response_prepare.append(self.add_cors_headers)

        # Static Files
        if os.path.exists(self.frontend_path):
            # Serve index.html at root
            self.app.router.add_get("/", self.serve_index)
            # Serve other static files
            self.app.router.add_static("/", self.frontend_path)
        else:
            print(f"Warning: Frontend path {self.frontend_path} does not exist.")
            self.app.router.add_get("/", self.handle_root_fallback)

    async def add_cors_headers(self, request, response):
        response.headers["Access-Control-Allow-Origin"] = "*"

    async def serve_index(self, request):
        return web.FileResponse(os.path.join(self.frontend_path, "index.html"))

    @web.middleware
    async def auth_middleware(self, request, handler):
        # Allow static files and auth endpoints
        if not request.path.startswith("/api/") or request.path.startswith(
            "/api/auth/"
        ):
            return await handler(request)

        # Check cookie
        session_id = request.cookies.get("LIMINAL_SESSION")
        if not session_id or session_id not in self.sessions:
            return web.json_response({"error": "Unauthorized"}, status=401)

        return await handler(request)

    async def handle_auth_challenge(self, request):
        try:
            data = await request.json()
            public_key = data.get("public_key")
            if not public_key:
                return web.json_response({"error": "Missing public_key"}, status=400)

            nonce = os.urandom(32).hex()
            self.challenges[public_key] = nonce
            return web.json_response({"nonce": nonce})
        except Exception:
            return web.json_response({"error": "Invalid request"}, status=400)

    async def handle_auth_verify(self, request):
        try:
            data = await request.json()
            public_key_hex = data.get("public_key")
            signature_hex = data.get("signature")

            if not public_key_hex or not signature_hex:
                return web.json_response({"error": "Missing params"}, status=400)

            nonce_hex = self.challenges.get(public_key_hex)
            if not nonce_hex:
                return web.json_response({"error": "No challenge found"}, status=400)

            # Verify Signature
            try:
                public_key = ed25519.Ed25519PublicKey.from_public_bytes(
                    bytes.fromhex(public_key_hex)
                )
                public_key.verify(
                    bytes.fromhex(signature_hex), bytes.fromhex(nonce_hex)
                )
            except Exception:
                return web.json_response({"error": "Invalid signature"}, status=401)

            # Check Authorization
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT 1 FROM authorized_identities WHERE public_key = ?",
                (public_key_hex,),
            )
            if not cursor.fetchone():
                return web.json_response(
                    {
                        "status": "unauthorized",
                        "error": "Identity verified but not authorized.",
                    }
                )

            # Issue Session
            session_id = str(uuid.uuid4())
            self.sessions[session_id] = public_key_hex

            # Cleanup challenge
            del self.challenges[public_key_hex]

            resp = web.json_response({"status": "ok"})
            resp.set_cookie(
                "LIMINAL_SESSION", session_id, httponly=True, samesite="Strict"
            )
            return resp

        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_auth_register(self, request):
        try:
            data = await request.json()
            public_key_hex = data.get("public_key")
            invite_code = data.get("invite_code")
            signature_hex = data.get("signature")

            if not public_key_hex or not invite_code or not signature_hex:
                return web.json_response({"error": "Missing params"}, status=400)

            # Check Invite Code
            if invite_code != self.invite_code:
                return web.json_response({"error": "Invalid invite code"}, status=401)

            # Verify Signature
            try:
                public_key = ed25519.Ed25519PublicKey.from_public_bytes(
                    bytes.fromhex(public_key_hex)
                )
                # The user signs the invite code to prove ownership of the key claiming the code
                public_key.verify(bytes.fromhex(signature_hex), invite_code.encode())
            except Exception:
                return web.json_response({"error": "Invalid signature"}, status=401)

            # Authorize
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO authorized_identities (public_key, created_at) VALUES (?, ?)",
                (public_key_hex, time.time()),
            )
            self.conn.commit()

            # Issue Session
            session_id = str(uuid.uuid4())
            self.sessions[session_id] = public_key_hex

            resp = web.json_response({"status": "registered"})
            resp.set_cookie(
                "LIMINAL_SESSION", session_id, httponly=True, samesite="Strict"
            )
            return resp

        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_logout(self, request):
        session_id = request.cookies.get("LIMINAL_SESSION")
        if session_id and session_id in self.sessions:
            del self.sessions[session_id]

        resp = web.json_response({"status": "logged_out"})
        resp.del_cookie("LIMINAL_SESSION")
        return resp

    async def handle_root_fallback(self, request):
        return web.Response(
            text="Dashboard Frontend not found. API is running.",
            content_type="text/plain",
        )

    async def handle_status(self, request):
        status = {
            "node_id": self.mesh.node_id,
            "topic": self.mesh.topic,
            "peers": list(self.mesh.peers),
            "running": self.mesh.running,
        }
        return web.json_response(status)

    async def handle_thoughts(self, request):
        return web.json_response(self.mesh.thoughts)

    async def handle_post_thought(self, request):
        try:
            data = await request.json()
            thought = data.get("thought")
            if not thought:
                return web.json_response({"error": "Missing 'thought'"}, status=400)

            await self.mesh.share_thought(thought)
            return web.json_response({"status": "ok"})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_batons(self, request):
        return web.json_response(self.mesh.advisory_batons())

    async def handle_post_baton(self, request):
        try:
            data = await request.json()
            resource = data.get("resource")
            action = data.get("action")

            if not resource or not action:
                return web.json_response(
                    {"error": "Missing 'resource' or 'action'"}, status=400
                )

            if action == "acquire":
                self.mesh.require_advisory_resource(resource)
                success = await self.mesh.acquire_baton(resource)
                return web.json_response({"success": success})
            elif action == "release":
                self.mesh.require_advisory_resource(resource)
                await self.mesh.release_baton(resource)
                return web.json_response({"status": "released"})
            else:
                return web.json_response({"error": "Invalid action"}, status=400)

        except ManagedTaskRefusal as error:
            return web.json_response({"error": str(error)}, status=409)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_kv(self, request):
        return web.json_response(self.mesh.get_all_kv())

    async def handle_backlog(self, request):
        backlog = self.mesh.get_kv("swarm_backlog") or []
        tasks = []
        for item in backlog:
            try:
                tasks.append(json.loads(item))
            except BaseException:
                continue
        return web.json_response(tasks)

    async def handle_post_backlog(self, request):
        try:
            data = await request.json()
            action = data.get("action")
            task_id = data.get("task_id")

            if action == "add":
                title = data.get("title")
                description = data.get("description", "")
                priority = data.get("priority", "medium")
                if not title:
                    return web.json_response({"error": "Missing title"}, status=400)

                task = {
                    "id": str(uuid.uuid4()),
                    "title": title,
                    "description": description,
                    "owner": None,
                    "status": "todo",
                    "priority": priority,
                }
                await self.mesh.update_set("swarm_backlog", json.dumps(task))
                return web.json_response({"status": "added", "task_id": task["id"]})

            elif action == "claim":
                if not task_id:
                    return web.json_response({"error": "Missing task_id"}, status=400)

                self.mesh.require_advisory_resource(f"task:{task_id}")
                success = await self.mesh.acquire_baton(f"task:{task_id}")
                if success:
                    backlog = self.mesh.get_kv("swarm_backlog") or []
                    for item in backlog:
                        try:
                            t = json.loads(item)
                            if t.get("id") == task_id:
                                self.mesh.require_advisory_resource(f"task:{task_id}")
                                t["owner"] = self.mesh.node_id
                                t["status"] = "in_progress"
                                await self.mesh.update_set(
                                    "swarm_backlog", json.dumps(t)
                                )
                                self.mesh.require_advisory_resource(f"task:{task_id}")
                                break
                        except ManagedTaskRefusal:
                            raise
                        except BaseException:
                            continue
                    return web.json_response({"status": "claimed"})
                else:
                    return web.json_response(
                        {"error": "Failed to claim task - baton denied"}, status=403
                    )

            elif action == "complete":
                if not task_id:
                    return web.json_response({"error": "Missing task_id"}, status=400)

                backlog = self.mesh.get_kv("swarm_backlog") or []
                self.mesh.require_advisory_resource(f"task:{task_id}")
                await self.mesh.release_baton(f"task:{task_id}")
                for item in backlog:
                    try:
                        t = json.loads(item)
                        if t.get("id") == task_id:
                            self.mesh.require_advisory_resource(f"task:{task_id}")
                            t["status"] = "done"
                            t["owner"] = self.mesh.node_id
                            await self.mesh.update_set("swarm_backlog", json.dumps(t))
                            self.mesh.require_advisory_resource(f"task:{task_id}")
                            break
                    except ManagedTaskRefusal:
                        raise
                    except BaseException:
                        continue
                return web.json_response({"status": "completed"})

            else:
                return web.json_response({"error": "Invalid action"}, status=400)

        except ManagedTaskRefusal as error:
            return web.json_response({"error": str(error)}, status=409)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_logs(self, request):
        return web.json_response(self.mesh.log_aggregator.get_logs())

    async def handle_network(self, request):
        return web.json_response(self.mesh.network_map)

    async def handle_discussions(self, request):
        discussions = {}
        for key in self.mesh.kv_store.keys():
            if key.startswith("chat:"):
                topic = key[5:]
                messages_raw = self.mesh.get_kv(key) or []
                messages = []
                for m_json in messages_raw:
                    try:
                        messages.append(json.loads(m_json))
                    except (json.JSONDecodeError, TypeError):
                        continue
                messages.sort(key=lambda x: x.get("timestamp", 0))
                discussions[topic] = messages
        return web.json_response(discussions)

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "0.0.0.0", self.port)
        await self.site.start()
        print(f"Dashboard Server started at http://localhost:{self.port}")

    async def stop(self):
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
