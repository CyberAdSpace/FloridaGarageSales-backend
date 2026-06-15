from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text
from sqlalchemy.sql import func

from app.database import Base


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, default="")
    price = Column(Float, nullable=False)
    category = Column(String(100), default="Other")
    image_url = Column(String(500), default="")
    image_urls = Column(Text, default="")  # JSON array of additional image URLs
    quantity = Column(Integer, default=1)
    condition = Column(String(50), default="Used - Good")
    is_active = Column(Boolean, default=True)
    is_featured = Column(Boolean, default=False)
    stripe_price_id = Column(String(255), default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    stripe_session_id = Column(String(255), default="")
    stripe_payment_intent = Column(String(255), default="")
    payment_method = Column(String(50), default="stripe")  # stripe or crypto
    crypto_tx_id = Column(String(255), default="")
    crypto_payer = Column(String(255), default="")
    crypto_token = Column(String(50), default="")
    customer_email = Column(String(255), default="")
    customer_name = Column(String(255), default="")
    total_amount = Column(Float, default=0.0)
    status = Column(String(50), default="pending")  # pending, paid, shipped, completed
    items_json = Column(Text, default="[]")  # JSON of ordered items
    shipping_address = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
