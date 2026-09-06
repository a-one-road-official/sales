from __future__ import annotations

import ast
from urllib.parse import urlparse, urlunparse


ALLOWED_IMPORTS = {
    "bs4",
    "re",
    "json",
    "html",
    "datetime",
    "urllib.parse",
    "collections",
    "itertools",
    "math",
    "typing",
}

DISALLOWED_CALL_NAMES = {
    "eval",
    "exec",
    "compile",
    "open",
    "__import__",
    "input",
}

DISALLOWED_ATTRS = {
    "system",
    "popen",
    "spawn",
    "fork",
    "remove",
    "unlink",
    "rmdir",
    "rmtree",
    "rename",
    "replace",
    "chmod",
    "chown",
    "connect",
    "send",
    "sendall",
}


class SafetyViolation(ValueError):
    pass


def canonicalize_url(url: str) -> str:
    p = urlparse((url or "").strip())
    scheme = (p.scheme or "https").lower()
    host = (p.hostname or "").lower()
    port = p.port
    netloc = host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    path = p.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", p.query, ""))


def same_host_or_subdomain(candidate: str, source_url: str) -> bool:
    c = (urlparse(candidate).hostname or "").lower().strip(".")
    s = (urlparse(source_url).hostname or "").lower().strip(".")
    if not c or not s:
        return False
    return c == s or c.endswith("." + s) or s.endswith("." + c)


def validate_adapter_code(code: str) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise SafetyViolation(f"syntax_error:{exc}") from exc

    has_extract = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "extract":
            has_extract = True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_IMPORTS:
                    raise SafetyViolation(f"import_not_allowed:{alias.name}")
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module not in ALLOWED_IMPORTS:
                raise SafetyViolation(f"import_not_allowed:{module}")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in DISALLOWED_CALL_NAMES:
                raise SafetyViolation(f"call_not_allowed:{node.func.id}")
            if isinstance(node.func, ast.Attribute) and node.func.attr in DISALLOWED_ATTRS:
                raise SafetyViolation(f"attr_call_not_allowed:{node.func.attr}")
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise SafetyViolation("global_state_not_allowed")

    if not has_extract:
        raise SafetyViolation("required_function_missing:extract")
