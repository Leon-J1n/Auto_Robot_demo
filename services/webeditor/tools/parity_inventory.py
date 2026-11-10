"""Inventory the caller surface (§5.9), the deleted legacy surface (§10.1) and the feature flags (§9.3).

Run from this repository alone: `python tools/parity_inventory.py`. It exits 1 if
a §5.9 route is not registered or a §10.1 route still is, so the generated
`docs/api-inventory.json` is a gate, not a description.

`--original <tree>` adds the ported-from project to the output. That tree is
audit material only: its `/api/commands` dispatch table is exactly what §10.1
deletes here, so its "commands" never become a target for this gateway.
"""
from __future__ import annotations
import argparse
import ast
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_gateway import BROWSER_ACTIONS

# §5.9, verbatim: the routes a caller may use against the local gateway.
CALLER_ROUTES = [
    ("GET", "/getWorld", "§4.1 live world, cache fallback"),
    ("POST", "/updateWorld", "§4.2 whole-world replace"),
    ("POST", "/updateItem", "§4.2 one object, full replacement"),
    ("POST", "/addObject", "§4.2 single object, browser allocates the id"),
    ("POST", "/removeObject", "§4.2/§12.3b cascade or detach"),
    ("POST", "/updateTraj", "§4.3 binary clip"),
    ("GET", "/captureScene", "§4.4 rgb / rgbd pixels"),
    ("GET", "/get2DAABB", "§4.5 projected box"),
    ("GET", "/events", "§5.1 the request/response channel"),
    ("GET", "/api/health", "§5.9 kept"),
    ("GET", "/api/runtimeConfig", "§9.1 constants + features"),
]

# §10.1, verbatim, in the shape the old registrations had (id segments as templates).
DELETED_ROUTES = [
    ("POST", "/api/commands"),
    ("GET", "/api/bootstrap"),
    ("GET", "/api/assets"),
    ("POST", "/api/assets/upload"),
    ("POST", "/api/assets/import-remote"),
    ("GET", "/api/assets/remote-proxy"),
    ("DELETE", "/api/assets/{id}"),
    ("GET", "/api/assets/{id}/file"),
    ("GET", "/api/assets/{id}/file/{path}"),
    ("GET", "/api/sensors/{entity_id}/frame"),
    ("POST", "/api/state/load-simstate"),
    ("GET", "/api/diagnostics"),
    ("GET", "/api/metrics"),
    ("GET", "/api/presentation"),
    ("POST", "/api/presentation/mode"),
    ("POST", "/api/agent-control/captures"),
    ("POST", "/api/agent-control/captures/{id}"),
    ("GET", "/api/timelines/{id}/frames/{i}"),
    ("GET", "/api/timelines/{id}/data"),
]

parser = argparse.ArgumentParser()
parser.add_argument("--original", type=Path, default=None,
                    help="ported-from tree, for the audit diff only")
args = parser.parse_args()
original = args.original / "web" if args.original is not None else None
output = ROOT / "docs"
output.mkdir(exist_ok=True)


def group(key: str) -> str:
    if key.startswith("agent"):
        return "Agent (kept as a placeholder, §5.9)"
    if key.startswith("viewport.load"):
        return "Manual load from disk (§4.1/§4.3)"
    if key.startswith("panels.assets"):
        return "Asset panel over OLS (§4.6)"
    if key.startswith("playback"):
        return "TRAJ playback (§4.3)"
    if key.startswith("panels.details.camera"):
        return "Camera details (§4.4)"
    if key.startswith("panels"):
        return "Panels, read-only (§4.7)"
    if key.startswith("navigation"):
        return "Left rail"
    if key.startswith("viewport"):
        return "Viewport"
    if key.startswith(("statusBar", "diagnostics")):
        return "Chrome and status"
    return "Browser UI"


def editor_text() -> str:
    sources = [path for path in sorted((ROOT / "editor" / "src").glob("*.ts"))
               if not path.name.endswith(".test.ts")]
    return "\n".join(path.read_text(encoding="utf-8") for path in sources)


def interpolated(references: str, key: str) -> bool:
    """True when source looks children of `key` up dynamically.

    `enabled(`navigation.assets.${GROUP_FEATURES[kind]}`)` never names
    `navigation.assets.presets` as a literal, so a plain string search would
    report those flags dead while the editor does read them.
    """
    needle = key + ".${"
    return any(quote + needle in references for quote in ('"', "'", "`"))


def reach(references: str, keys: list[str], key: str) -> str:
    """How the editor can consult one flag, given logic.ts:featureEnabled.

    `literal` names the key in source; `interpolated` reads it through a template
    lookup of itself or of one of its ancestors; `gate_ancestor` is neither but
    turns a referenced descendant off when set to false; `unreferenced` is dead
    weight §9.3 should have removed.
    """
    if f'"{key}"' in references:
        return "literal"
    if any(interpolated(references, prefix) for prefix in
           [key] + [key.rsplit(".", edge)[0] for edge in range(1, key.count("."))]):
        return "interpolated"
    if any(other != key and other.startswith(f"{key}.") and reach(references, keys, other) != "unreferenced"
           for other in keys):
        return "gate_ancestor"
    return "unreferenced"


def status(key: str, *, local: bool, in_original: bool | None, reached: str) -> str:
    if not local:
        return "REMOVED_BY_V2"
    if key.startswith("agent"):
        return "PLACEHOLDER_KEPT"
    if reached == "unreferenced":
        return "DECLARED_UNWIRED"
    if in_original is False:
        return "NEW_IN_V2"
    return "GATE_ANCESTOR" if reached == "gate_ancestor" else "WIRED"


local_flags = json.loads((ROOT / "features.json").read_text(encoding="utf-8"))["features"]
original_flags = (json.loads((original / "config/features.json").read_text(encoding="utf-8"))["features"]
                  if original is not None else {})
references = editor_text()
all_keys = sorted(set(local_flags) | set(original_flags))
with (output / "feature-inventory.csv").open("w", encoding="utf-8-sig", newline="") as file:
    writer = csv.writer(file)
    writer.writerow(["feature", "local_config", "original_config", "editor_reach", "group", "audit_status"])
    for key in all_keys:
        reached = reach(references, all_keys, key)
        writer.writerow([key, local_flags.get(key, "absent"), original_flags.get(key, "absent"),
                         reached, group(key),
                         status(key, local=key in local_flags,
                               in_original=(key in original_flags) if original is not None else None,
                               reached=reached)])


def routes(path: Path) -> list[dict[str, object]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not node.func.attr.startswith("add_") or not node.args:
            continue
        if ast.unparse(node.func.value) != "app.router":
            continue
        if not isinstance(node.args[0], ast.Constant):
            continue
        result.append({"method": node.func.attr[4:].upper(), "path": node.args[0].value,
                       "line": node.lineno, "handler": ast.unparse(node.args[1]) if len(node.args) > 1 else ""})
    return sorted(result, key=lambda item: (item["path"], item["method"]))


def legacy_commands(path: Path) -> list[str]:
    """The original tree's `/api/commands` method table - what §10.1 deletes here."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "handlers" and isinstance(node.value, ast.Dict):
            names.update(key.value for key in node.value.keys if isinstance(key, ast.Constant))
    return sorted(name for name in names if isinstance(name, str))


local_routes = routes(ROOT / "local_gateway.py")
registered = {(route["method"], route["path"]) for route in local_routes}
missing = [{"method": method, "path": path, "spec": spec}
           for method, path, spec in CALLER_ROUTES if (method, path) not in registered]
still_live = [{"method": method, "path": path} for method, path in DELETED_ROUTES
              if (method, path) in registered]
data = {
    "note": "Registration inventory, not proof of behavioural parity: the gateway is a "
            "channel and the browser owns the scene (§1.1), so behaviour is audited by "
            "parity-probes.json (no browser) and browser_smoke.cjs (real browser).",
    "caller_routes": [{"method": method, "path": path, "spec": spec,
                       "registered": (method, path) in registered}
                      for method, path, spec in CALLER_ROUTES],
    "agent_routes_kept": [route for route in local_routes if str(route["path"]).startswith("/api/agent/")],
    "deleted_routes": [{"method": method, "path": path,
                        "still_registered": (method, path) in registered}
                       for method, path in DELETED_ROUTES],
    "local_routes": local_routes,
    "browser_actions": sorted(BROWSER_ACTIONS),
    "original_routes": routes(original / "gateway/server.py") if original is not None else None,
    "original_commands": legacy_commands(original / "gateway/scene.py") if original is not None else None,
}
(output / "api-inventory.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"caller_routes": len(CALLER_ROUTES), "missing_caller_routes": missing,
                  "deleted_routes": len(DELETED_ROUTES), "still_registered": still_live,
                  "local_routes": len(local_routes), "browser_actions": len(data["browser_actions"]),
                  "features_local": len(local_flags), "features_original": len(original_flags),
                  "features_unreferenced": sorted(key for key in local_flags
                                                  if reach(references, all_keys, key) == "unreferenced")},
                 indent=2))
sys.exit(1 if missing or still_live else 0)
