import asyncio
import os
import sys
import argparse

sys.path.insert(0, os.path.abspath("src"))
from liminal_bridge.mesh import LiminalMesh
from liminal_bridge.auth import get_or_create_swarm_key

try:
    from src.core.collaboration.experimental_boundary import (
        MANAGED_TASK_REFUSAL,
        ManagedTaskRefusal,
        managed_task_resource,
    )
except ImportError:
    from core.collaboration.experimental_boundary import (
        MANAGED_TASK_REFUSAL,
        ManagedTaskRefusal,
        managed_task_resource,
    )


async def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage granular file locks")
    parser.add_argument(
        "action", choices=["acquire", "release"], help="Action to perform"
    )
    parser.add_argument("filepath", type=str, help="The file path to lock or unlock")

    args = parser.parse_args(argv)
    if managed_task_resource(args.filepath):
        print(f"DENIED: {MANAGED_TASK_REFUSAL}")
        return 1

    swarm_key = get_or_create_swarm_key()
    mesh = LiminalMesh(secret_key=swarm_key)

    try:
        mesh.require_advisory_resource(args.filepath)
        await mesh.start()
        if args.action == "acquire":
            success = await mesh.acquire_baton(args.filepath)
            if success:
                print(f"SUCCESS: Baton acquired for {args.filepath}")
            else:
                owner = mesh.batons.get(args.filepath, "unknown")
                print(f"DENIED: {args.filepath} is currently locked by {owner}.")
                return 1
        elif args.action == "release":
            await mesh.release_baton(args.filepath)
            print(f"SUCCESS: Baton released for {args.filepath}")
    except ManagedTaskRefusal as error:
        print(f"DENIED: {error}")
        return 1
    finally:
        await mesh.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
