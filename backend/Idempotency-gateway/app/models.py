"""Pydantic models for request/response validation."""

from pydantic import BaseModel, Field


class PaymentRequest(BaseModel):
    """Incoming payment request body."""

    amount: int = Field(..., gt=0, description="The payment amount (must be positive)")
    currency: str = Field(..., min_length=1, description="Currency code, e.g. GHS, USD")


class PaymentResponse(BaseModel):
    """Successful payment response."""

    status: str
    message: str


class ErrorResponse(BaseModel):
    """Error response for conflict / validation errors."""

    detail: str
