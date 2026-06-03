"""
API Authentication Middleware
Implements API key-based authentication with rate limiting
"""

import hashlib
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple

import asyncpg
from fastapi import HTTPException, Request, status
from fastapi.security import APIKeyHeader

# API Key header scheme
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def hash_api_key(key: str) -> str:
    """Hash an API key using SHA-256"""
    return hashlib.sha256(key.encode()).hexdigest()


def generate_api_key() -> str:
    """Generate a secure random API key"""
    return f"docint_{secrets.token_urlsafe(32)}"


async def verify_api_key(
    pool: asyncpg.Pool,
    api_key: Optional[str],
    request: Request,
) -> Optional[int]:
    """
    Verify API key and check rate limits.
    Returns api_key_id if valid, raises HTTPException if invalid.
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Include X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    key_hash = hash_api_key(api_key)

    async with pool.acquire() as conn:
        # Get API key details
        key_record = await conn.fetchrow(
            """
            SELECT id, is_active, rate_limit, expires_at
            FROM api_keys
            WHERE key_hash = $1
            """,
            key_hash,
        )

        if not key_record:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
            )

        if not key_record["is_active"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key is inactive",
            )

        # Check expiration
        if key_record["expires_at"] and key_record["expires_at"] < datetime.utcnow():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key has expired",
            )

        # Rate limiting check
        api_key_id = key_record["id"]
        rate_limit = key_record["rate_limit"]

        # Count requests in the last hour
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        request_count = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM api_requests
            WHERE api_key_id = $1 AND created_at > $2
            """,
            api_key_id,
            one_hour_ago,
        )

        if request_count >= rate_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Limit: {rate_limit} requests/hour",
                headers={"Retry-After": "3600"},
            )

        # Update last used timestamp
        await conn.execute(
            """
            UPDATE api_keys
            SET last_used_at = CURRENT_TIMESTAMP,
                usage_count = usage_count + 1
            WHERE id = $1
            """,
            api_key_id,
        )

    return api_key_id


async def log_api_request(
    pool: asyncpg.Pool,
    api_key_id: Optional[int],
    request: Request,
    status_code: int,
    response_time_ms: int,
    error_message: Optional[str] = None,
):
    """Log API request to database for audit trail"""
    try:
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO api_requests (
                    api_key_id,
                    endpoint,
                    method,
                    status_code,
                    response_time_ms,
                    ip_address,
                    user_agent,
                    error_message
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                api_key_id,
                str(request.url.path),
                request.method,
                status_code,
                response_time_ms,
                client_ip,
                user_agent,
                error_message,
            )
    except Exception as e:
        # Don't fail the request if logging fails
        print(f"Failed to log API request: {e}")


class AuthMiddleware:
    """
    Middleware to handle authentication and request logging
    """

    def __init__(self, pool: asyncpg.Pool, exempt_paths: list[str] = None):
        self.pool = pool
        self.exempt_paths = exempt_paths or [
            "/",
            "/docs",
            "/openapi.json",
            "/redoc",
            "/dashboard",
            "/metrics",  # Public metrics endpoint
        ]

    async def __call__(self, request: Request, call_next):
        start_time = time.time()
        api_key_id = None
        status_code = 200
        error_message = None

        # Skip auth for exempt paths
        if any(request.url.path.startswith(path) for path in self.exempt_paths):
            response = await call_next(request)
            return response

        try:
            # Verify API key
            api_key = request.headers.get("X-API-Key")
            api_key_id = await verify_api_key(self.pool, api_key, request)

            # Attach api_key_id to request state for use in endpoints
            request.state.api_key_id = api_key_id

            # Process request
            response = await call_next(request)
            status_code = response.status_code

        except HTTPException as e:
            status_code = e.status_code
            error_message = e.detail
            raise

        except Exception as e:
            status_code = 500
            error_message = str(e)
            raise

        finally:
            # Log request
            response_time_ms = int((time.time() - start_time) * 1000)
            await log_api_request(
                self.pool,
                api_key_id,
                request,
                status_code,
                response_time_ms,
                error_message,
            )

        return response


# Helper functions for API key management endpoints

async def create_api_key(
    pool: asyncpg.Pool,
    key_name: str,
    created_by: str = "admin",
    rate_limit: int = 100,
    expires_in_days: Optional[int] = None,
) -> Tuple[str, int]:
    """
    Create a new API key.
    Returns (plaintext_key, key_id)
    """
    plaintext_key = generate_api_key()
    key_hash = hash_api_key(plaintext_key)

    expires_at = None
    if expires_in_days:
        expires_at = datetime.utcnow() + timedelta(days=expires_in_days)

    async with pool.acquire() as conn:
        key_id = await conn.fetchval(
            """
            INSERT INTO api_keys (
                key_hash,
                key_name,
                created_by,
                rate_limit,
                expires_at
            ) VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            key_hash,
            key_name,
            created_by,
            rate_limit,
            expires_at,
        )

    return plaintext_key, key_id


async def list_api_keys(pool: asyncpg.Pool, include_inactive: bool = False):
    """List all API keys (without revealing the actual keys)"""
    async with pool.acquire() as conn:
        query = """
            SELECT 
                id,
                key_name,
                created_by,
                is_active,
                rate_limit,
                created_at,
                last_used_at,
                expires_at,
                usage_count
            FROM api_keys
        """
        if not include_inactive:
            query += " WHERE is_active = true"

        query += " ORDER BY created_at DESC"

        rows = await conn.fetch(query)
        return [dict(row) for row in rows]


async def revoke_api_key(pool: asyncpg.Pool, key_id: int) -> bool:
    """Revoke (deactivate) an API key"""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE api_keys
            SET is_active = false
            WHERE id = $1
            """,
            key_id,
        )
        return result.split()[-1] == "1"  # Check if 1 row was updated
