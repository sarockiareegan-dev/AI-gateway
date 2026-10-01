"""Generate the Agami license signing key pair and issue licenses.

Keep the private key offline. Only the public key ships, at
``litellm/proxy/auth/agami_license_public_key.pem``.

    python scripts/agami_license.py keygen --private-key ~/.agami/license_signing_key.pem \
        --public-key litellm/proxy/auth/agami_license_public_key.pem
    python scripts/agami_license.py issue --private-key ~/.agami/license_signing_key.pem \
        --customer example-customer --expires 2027-12-31 --features "*" --max-users 500 --max-teams 50
"""

import argparse
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Final, Literal

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from litellm.proxy.auth.entitlements import LICENSE_ALGORITHM, LICENSE_ISSUER


class _KeygenArgs(BaseModel):
    model_config = ConfigDict(frozen=True)

    command: Literal["keygen"]
    private_key: Path
    public_key: Path


class _IssueArgs(BaseModel):
    model_config = ConfigDict(frozen=True)

    command: Literal["issue"]
    private_key: Path
    customer: str
    expires: datetime
    features: str
    max_users: int | None
    max_teams: int | None


_CLI_ARGS: Final[TypeAdapter[_KeygenArgs | _IssueArgs]] = TypeAdapter(
    Annotated[_KeygenArgs | _IssueArgs, Field(discriminator="command")]
)


def keygen(private_key_path: Path, public_key_path: Path) -> None:
    if private_key_path.exists():
        sys.exit(f"Refusing to overwrite existing private key at {private_key_path}")
    private_key: Final = Ed25519PrivateKey.generate()
    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    print(f"Private key: {private_key_path}\nPublic key:  {public_key_path}")  # noqa: T201


def issue(
    private_key_path: Path,
    customer: str,
    expires: datetime,
    features: tuple[str, ...],
    max_users: int | None,
    max_teams: int | None,
) -> str:
    private_key: Final = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        sys.exit(f"{private_key_path} is not an Ed25519 private key")
    claims: Final = {
        "iss": LICENSE_ISSUER,
        "sub": customer,
        "jti": str(uuid.uuid4()),
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int(expires.timestamp()),
        "features": list(features),
        **({"max_users": max_users} if max_users is not None else {}),
        **({"max_teams": max_teams} if max_teams is not None else {}),
    }
    return jwt.encode(claims, private_key, algorithm=LICENSE_ALGORITHM)


def _end_of_day_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)


def main() -> None:
    parser: Final = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands: Final = parser.add_subparsers(dest="command", required=True)

    keygen_parser: Final = commands.add_parser("keygen", help="create a new signing key pair")
    keygen_parser.add_argument("--private-key", type=Path, required=True)
    keygen_parser.add_argument("--public-key", type=Path, required=True)

    issue_parser: Final = commands.add_parser("issue", help="sign a license and print it")
    issue_parser.add_argument("--private-key", type=Path, required=True)
    issue_parser.add_argument("--customer", required=True)
    issue_parser.add_argument("--expires", type=_end_of_day_utc, required=True, help="last valid day, YYYY-MM-DD")
    issue_parser.add_argument("--features", default="*", help='comma-separated feature names, or "*" for all')
    issue_parser.add_argument("--max-users", type=int)
    issue_parser.add_argument("--max-teams", type=int)

    match _CLI_ARGS.validate_python(vars(parser.parse_args())):
        case _KeygenArgs() as args:
            keygen(args.private_key.expanduser(), args.public_key)
        case _IssueArgs() as args:
            print(  # noqa: T201
                issue(
                    private_key_path=args.private_key.expanduser(),
                    customer=args.customer,
                    expires=args.expires,
                    features=tuple(f.strip() for f in args.features.split(",") if f.strip()),
                    max_users=args.max_users,
                    max_teams=args.max_teams,
                )
            )


if __name__ == "__main__":
    main()
