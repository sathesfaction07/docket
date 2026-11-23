from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CF_")

    database_backend: str = "dynamodb"  # "dynamodb" | "firestore"
    aws_region: str = "us-east-1"
    dynamodb_table: str = "Docket"
    gcp_project: str = ""
    firestore_collection: str = "cases"

    # Identity is resolved PER REQUEST from a bearer token, never from the server process.
    # An identity taken from the process (the old `getpass.getuser()` default) is the same
    # for every client, so ownership checks would compare the server's user with itself.
    #
    #   CF_API_TOKENS="tok-alice:alice,tok-bob:bob"   one token per person
    #   CF_LOCAL_USER="alice"                         single-user local mode: a request with
    #                                                 NO Authorization header acts as alice.
    #                                                 Leave empty on any shared host.
    api_tokens: str = ""
    local_user: str = ""
    admin_users: str = "admin"
    cors_origins: str = "http://localhost:3000"

    @property
    def admin_list(self) -> list[str]:
        return [u.strip() for u in self.admin_users.split(",") if u.strip()]

    @property
    def token_map(self) -> dict[str, str]:
        tokens: dict[str, str] = {}
        for entry in self.api_tokens.split(","):
            token, sep, user = entry.strip().partition(":")
            if sep and token.strip() and user.strip():
                tokens[token.strip()] = user.strip()
        return tokens

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    log_level: str = "INFO"


settings = Settings()
