import asyncio
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.models import PaymentRequest, PaymentResponse, ErrorResponse
from app.store import IdempotencyEntry, compute_body_hash, idempotency_store

app = FastAPI(
    title="Idempotency Gateway",
    description="A Pay-Once Protocol implementation to prevent double-charging.",
    version="1.0.0",
)


@app.post(
    "/process-payment",
    response_model=PaymentResponse,
    status_code=201,
    responses={
        400: {"model": ErrorResponse, "description": "Missing Idempotency-Key header"},
        409: {"model": ErrorResponse, "description": "Idempotency key already used for a different request body"},
        422: {"description": "Validation Error"},
    },
)
async def process_payment(
    request: Request,
    payment_request: PaymentRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", description="Unique key for idempotency"),
):
    """
    Process a payment request with idempotency guarantees.
    
    - First request: Processed normally (simulated 2s delay).
    - Duplicate request: Returns cached response with X-Cache-Hit header.
    - Duplicate key but different body: Returns 409 Conflict.
    - Concurrent duplicate request: Blocks until the first request completes.
    """
    # 1. Hash the request body
    body_dict = payment_request.model_dump()
    request_hash = compute_body_hash(body_dict)

    # 2. Acquire a lock for this specific idempotency key
    lock = await idempotency_store.get_lock(idempotency_key)
    
    async with lock:
        entry = idempotency_store.get(idempotency_key)

        if entry:
            # Found an existing entry. Check for payload mismatch (Story 3)
            if entry.request_body_hash != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key already used for a different request body.",
                )

            # Bonus Story: The In-Flight Check
            # If the request is currently processing, wait for it to finish
            if entry.state == "processing":
                # We need to release the lock while waiting so we don't deadlock
                pass
            else:
                # Fast path: it's completed, return the cached response immediately (Story 2)
                return JSONResponse(
                    content=entry.response_body,
                    status_code=entry.status_code,
                    headers={"X-Cache-Hit": "true"},
                )

    # If we are here and there's an entry, it means it's processing
    if entry and entry.state == "processing":
        # Wait for the first request to complete without holding the lock
        await entry.event.wait()
        
        # After waiting, fetch the updated entry to get the cached response
        entry = idempotency_store.get(idempotency_key)
        return JSONResponse(
            content=entry.response_body,
            status_code=entry.status_code,
            headers={"X-Cache-Hit": "true"},
        )

    # 3. No existing entry. This is the first request (Story 1)
    # Create a new entry and store it in "processing" state
    new_entry = IdempotencyEntry(request_body_hash=request_hash, state="processing")
    
    async with lock:
        # Double-check inside the lock in case another request snuck in
        existing_entry = idempotency_store.get(idempotency_key)
        if existing_entry:
            # If someone else created it while we were waiting, just recursively handle it
            return await process_payment(request, payment_request, idempotency_key)
            
        idempotency_store.set(idempotency_key, new_entry)

    # Simulate processing (2-second delay)
    await asyncio.sleep(2.0)

    # Prepare the successful response
    response_data = {
        "status": "success",
        "message": f"Charged {payment_request.amount} {payment_request.currency}",
    }
    status_code = 201

    # 4. Update the store with the completed response and notify waiters
    async with lock:
        new_entry.state = "completed"
        new_entry.status_code = status_code
        new_entry.response_body = response_data
        
        # Unblock any concurrent requests waiting on this entry
        new_entry.event.set()

    return JSONResponse(
        content=response_data,
        status_code=status_code,
    )


# Background task to clean up expired idempotency keys
import asyncio

async def cleanup_task():
    while True:
        try:
            # Run cleanup every 10 minutes
            await asyncio.sleep(600)
            removed = idempotency_store.cleanup_expired()
            if removed > 0:
                print(f"Cleaned up {removed} expired idempotency keys.")
        except Exception as e:
            print(f"Error in cleanup task: {e}")

@app.on_event("startup")
async def startup_event():
    # Start the background cleanup task
    asyncio.create_task(cleanup_task())

