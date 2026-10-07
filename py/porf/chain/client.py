"""On-chain client. The ONLY module the rest of the Python code uses to talk to the program.

It shells out to the TypeScript CLI in ./cli (built on @anchor-lang/core and the generated IDL), which
prints one JSON object per call. Errors become `ChainError` carrying the Anchor error name, resolved from
the IDL at runtime. See docs/ENV.md for why there is no anchorpy.

Environment: PORF_RPC_URL (default http://127.0.0.1:8899), PORF_IDL (default <repo>/target/idl/...json),
PORF_NODE (path to a node >= 20 binary; default: the version in .node-version via fnm, else `node` on PATH).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_DIR = Path(__file__).resolve().parent / "cli"
_TSX = CLI_DIR / "node_modules" / "tsx" / "dist" / "cli.mjs"
_ENTRY = CLI_DIR / "src" / "cli.ts"


class ChainError(Exception):
    """A failed chain call. `name` is the program/Anchor error name, e.g. "InvalidStatus"."""

    def __init__(self, name: str, message: str, code: int | None = None, logs: list[str] | None = None, kind: str = "other"):
        super().__init__(f"{name}: {message}")
        self.name, self.message, self.code, self.logs, self.kind = name, message, code, logs or [], kind

    @classmethod
    def from_json(cls, err: dict[str, Any]) -> "ChainError":
        return cls(err.get("name", "Error"), err.get("message", ""), err.get("code"), err.get("logs"), err.get("kind", "other"))


# ---------------------------------------------------------------- typed views of on-chain accounts

def _int(v: Any) -> int:
    return int(v)


@dataclass(frozen=True)
class Config:
    admin: str
    verifier: str
    mint: str
    stake_bps: int
    bump: int


@dataclass(frozen=True)
class Request:
    requester: str
    request_id: int
    mint: str
    scene_type: int
    seed: int
    num_frames: int
    width: int
    height: int
    occlusion_level: int
    price_per_frame: int
    escrow_amount: int
    stake_amount: int
    claim_duration: int
    commit_duration: int
    challenge_window_secs: int
    node: str
    status: str  # "Open" | "Claimed" | "Committed" | "Challenged" | "Verified" | "Paid" | "Slashed" | "Expired"
    created_at: int
    claimed_at: int
    claim_deadline: int
    commit_deadline: int
    challenge_window_end: int
    bump: int


@dataclass(frozen=True)
class Submission:
    request: str
    node: str
    merkle_root: str  # hex
    frame_count: int
    commit_slot: int
    sample_seed: str  # hex
    sample_slot: int  # slot of the SlotHashes entry that fed sample_seed; 0 until open_challenge
    sample_count: int
    verdict: str  # "Pending" | "Pass" | "Fail"
    bump: int


_STR_FIELDS = {"admin", "verifier", "mint", "requester", "node", "request", "merkle_root", "sample_seed", "status", "verdict"}


def _build(cls: type, data: dict[str, Any]):
    """Strict: an unknown or missing field means the Python view and the program have drifted."""
    names = {f.name for f in fields(cls)}
    if set(data) != names:
        raise ChainError("SchemaMismatch", f"{cls.__name__} fields differ from the program's: extra={sorted(set(data) - names)} missing={sorted(names - set(data))}")
    return cls(**{k: (v if k in _STR_FIELDS else _int(v)) for k, v in data.items()})


@dataclass(frozen=True)
class RequestView:
    address: str
    escrow: str
    stake_vault: str
    submission: str
    escrow_balance: int | None
    stake_vault_balance: int | None
    request: Request


# Demo default for the challenge window: 60 s. The worst end-to-end verification of a 20-frame request measured 16.9 s under CPU load,
# 3 x 16.9 = 51 s, rounded UP to 60 s because screen recording during the demo adds CPU load (docs/PROTOCOL.md section 17.6).
DEFAULT_CHALLENGE_WINDOW_SECS = 60


@dataclass(frozen=True)
class CreateRequestParams:
    request_id: int
    seed: int
    num_frames: int
    price_per_frame: int
    claim_duration: int
    commit_duration: int
    challenge_window_secs: int = DEFAULT_CHALLENGE_WINDOW_SECS
    scene_type: int = 0
    width: int = 640
    height: int = 480
    occlusion_level: int = 0


# ---------------------------------------------------------------- client

def _find_node() -> str:
    if os.environ.get("PORF_NODE"):
        return os.environ["PORF_NODE"]
    version = (REPO_ROOT / ".node-version").read_text().strip() if (REPO_ROOT / ".node-version").exists() else None
    if version:
        bases = [Path(os.environ["FNM_DIR"])] if os.environ.get("FNM_DIR") else []
        bases += [Path.home() / ".local/share/fnm", Path.home() / "Library/Application Support/fnm"]
        for base in bases:
            cand = base / "node-versions" / f"v{version}" / "installation" / "bin" / "node"
            if cand.exists():
                return str(cand)
    found = shutil.which("node")
    if not found:
        raise ChainError("NodeNotFound", "node not found; install Node 22 (docs/ENV.md) or set PORF_NODE")
    return found


def _prep(value: Any) -> Any:
    """Make Python values JSON-safe for the CLI: ints become decimal strings (u64 must not pass through a JS number)."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _prep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_prep(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        return {f.name: _prep(getattr(value, f.name)) for f in fields(value)}
    return value


class ChainClient:
    def __init__(self, rpc_url: str | None = None, idl_path: str | os.PathLike | None = None, timeout: float = 90.0):
        self.rpc_url = rpc_url or os.environ.get("PORF_RPC_URL", "http://127.0.0.1:8899")
        self.idl_path = str(idl_path) if idl_path else os.environ.get("PORF_IDL")
        self.timeout = timeout

    # -- plumbing
    def call(self, command: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        if not _TSX.exists():
            raise ChainError("CliNotInstalled", f"run `npm ci` in {CLI_DIR} first")
        env = dict(os.environ, PORF_RPC_URL=self.rpc_url)
        if self.idl_path:
            env["PORF_IDL"] = self.idl_path
        cmd = [_find_node(), str(_TSX), str(_ENTRY), command, json.dumps(_prep(args or {}))]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, env=env, cwd=str(REPO_ROOT))
        except subprocess.TimeoutExpired as e:
            raise ChainError("Timeout", f"{command} took longer than {self.timeout}s") from e
        lines = [l for l in proc.stdout.strip().splitlines() if l.strip()]
        try:
            out = json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError):
            raise ChainError("CliCrash", f"{command} produced no JSON (exit {proc.returncode}): {proc.stderr.strip()[-400:] or proc.stdout.strip()[-400:]}")
        if not out.get("ok"):
            raise ChainError.from_json(out.get("error", {}))
        out.pop("ok")
        return out

    @staticmethod
    def _which(request: str | None, requester: str | None, request_id: int | None) -> dict[str, Any]:
        if request:
            return {"request": request}
        if requester is not None and request_id is not None:
            return {"requester": requester, "request_id": request_id}
        raise ChainError("InvalidInput", "give either `request` or both `requester` and `request_id`", kind="input")

    # -- reads
    def pubkey(self, signer: str) -> str:
        """Public key of a keypair file."""
        return self.call("pubkey", {"signer": signer})["pubkey"]

    def info(self) -> dict[str, Any]:
        return self.call("info")

    def get_config(self) -> Config:
        return _build(Config, self.call("get-config")["account"])

    def get_request(self, request: str | None = None, requester: str | None = None, request_id: int | None = None) -> RequestView:
        d = self.call("get-request", self._which(request, requester, request_id))
        return RequestView(
            address=d["address"], escrow=d["escrow"], stake_vault=d["stake_vault"], submission=d["submission"],
            escrow_balance=None if d["escrow_balance"] is None else int(d["escrow_balance"]),
            stake_vault_balance=None if d["stake_vault_balance"] is None else int(d["stake_vault_balance"]),
            request=_build(Request, d["account"]),
        )

    def get_submission(self, request: str | None = None, requester: str | None = None, request_id: int | None = None) -> Submission:
        return _build(Submission, self.call("get-submission", self._which(request, requester, request_id))["account"])

    def list_requests(self, status: str | None = None, requester: str | None = None) -> list[tuple[str, Request]]:
        args = {k: v for k, v in (("status", status), ("requester", requester)) if v}
        return [(r["address"], _build(Request, r["account"])) for r in self.call("list-requests", args)["requests"]]

    # -- instructions (each returns the CLI's JSON, which includes the transaction `signature`)
    def init_config(self, signer: str, mint: str, verifier: str, stake_bps: int) -> dict[str, Any]:
        return self.call("init-config", {"signer": signer, "mint": mint, "verifier": verifier, "stake_bps": stake_bps})

    def create_request(self, signer: str, params: CreateRequestParams | dict[str, Any], mint: str | None = None, requester_token: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"signer": signer, "params": params}
        if mint:
            args["mint"] = mint
        if requester_token:
            args["requester_token"] = requester_token
        return self.call("create-request", args)

    def stake_and_claim(self, signer: str, request: str | None = None, requester: str | None = None, request_id: int | None = None,
                        mint: str | None = None, node_token: str | None = None) -> dict[str, Any]:
        args = {"signer": signer, **self._which(request, requester, request_id)}
        args.update({k: v for k, v in (("mint", mint), ("node_token", node_token)) if v})
        return self.call("stake-and-claim", args)

    def submit_commitment(self, signer: str, merkle_root: bytes | str, frame_count: int, request: str | None = None,
                          requester: str | None = None, request_id: int | None = None) -> dict[str, Any]:
        root = merkle_root.hex() if isinstance(merkle_root, (bytes, bytearray)) else merkle_root
        return self.call("submit-commitment", {"signer": signer, "merkle_root": root, "frame_count": frame_count,
                                                **self._which(request, requester, request_id)})

    def open_challenge(self, signer: str, request: str | None = None, requester: str | None = None, request_id: int | None = None,
                       max_attempts: int = 1, delay: float = 0.4) -> dict[str, Any]:
        """Open the challenge (verifier key). The program rejects it with SlotHashNotNewer until the SlotHashes sysvar holds
        a slot newer than the commit, i.e. a slot or two after the commit. With max_attempts > 1 this retries ONLY that error,
        `delay` seconds apart, and raises a clear ChainError if the attempts run out. Default: one attempt, no retry."""
        args = {"signer": signer, **self._which(request, requester, request_id)}
        for attempt in range(1, max_attempts + 1):
            try:
                return self.call("open-challenge", args)
            except ChainError as e:
                if e.name != "SlotHashNotNewer":
                    raise
                if attempt == max_attempts:
                    if max_attempts == 1:
                        raise
                    raise ChainError("SlotHashNotNewer", f"open_challenge was still rejected with SlotHashNotNewer after {max_attempts} attempts "
                                     f"{delay}s apart; the chain has not produced a slot newer than the commit (is the validator running?)",
                                     e.code, e.logs, e.kind) from e
                time.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def submit_verdict(self, signer: str, verdict: str, request: str | None = None, requester: str | None = None,
                       request_id: int | None = None) -> dict[str, Any]:
        """verdict is "Pass" or "Fail" (verifier key). "Pending" is passed through so the program can reject it."""
        return self.call("submit-verdict", {"signer": signer, "verdict": verdict, **self._which(request, requester, request_id)})

    def finalize_payout(self, signer: str, request: str | None = None, requester: str | None = None, request_id: int | None = None,
                        node_token: str | None = None, mint: str | None = None) -> dict[str, Any]:
        """Pay escrow + stake to the node. Anyone may sign. node_token defaults to the node's associated token account."""
        args = {"signer": signer, **self._which(request, requester, request_id)}
        args.update({k: v for k, v in (("node_token", node_token), ("mint", mint)) if v})
        return self.call("finalize-payout", args)

    def slash_and_refund(self, signer: str, request: str | None = None, requester: str | None = None, request_id: int | None = None,
                         requester_token: str | None = None, mint: str | None = None) -> dict[str, Any]:
        """Refund escrow + the node's stake to the requester after a Fail verdict. Anyone may sign."""
        args = {"signer": signer, **self._which(request, requester, request_id)}
        args.update({k: v for k, v in (("requester_token", requester_token), ("mint", mint)) if v})
        return self.call("slash-and-refund", args)

    # -- dev utilities (localnet setup and tests)
    def create_mint(self, signer: str, mint_keypair: str | None = None, decimals: int = 6) -> str:
        args: dict[str, Any] = {"signer": signer, "decimals": decimals}
        if mint_keypair:
            args["mint_keypair"] = mint_keypair
        return self.call("create-mint", args)["mint"]

    def create_token_account(self, signer: str, mint: str, owner: str | None = None) -> str:
        args = {"signer": signer, "mint": mint, **({"owner": owner} if owner else {})}
        return self.call("create-token-account", args)["token_account"]

    def mint_to(self, signer: str, mint: str, amount: int, token_account: str | None = None, owner: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"signer": signer, "mint": mint, "amount": amount}
        args.update({k: v for k, v in (("token_account", token_account), ("owner", owner)) if v})
        return self.call("mint-to", args)

    def token_balance(self, token_account: str | None = None, owner: str | None = None, mint: str | None = None) -> int:
        args = {"token_account": token_account} if token_account else {"owner": owner, "mint": mint}
        return int(self.call("token-balance", args)["amount"])

    def airdrop(self, to: str, sol: float) -> dict[str, Any]:
        return self.call("airdrop", {"to": to, "sol": sol})
