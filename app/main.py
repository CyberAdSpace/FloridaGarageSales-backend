import json
import os
import smtplib
import uuid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
import threading

import stripe
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.config import (
    STRIPE_SECRET_KEY,
    STRIPE_PUBLISHABLE_KEY,
    STRIPE_WEBHOOK_SECRET,
    FRONTEND_URL,
    UPLOAD_DIR,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USER,
    SMTP_PASSWORD,
    NOTIFICATION_EMAIL,
)
from app.database import engine, get_db, Base
from app.models import Product, Order
from app.auth import authenticate_admin, create_access_token, get_current_admin

# Create tables
Base.metadata.create_all(bind=engine)

# Migrate: add missing columns to existing tables
from sqlalchemy import text, inspect as sa_inspect
with engine.connect() as conn:
    inspector = sa_inspect(engine)
    existing_order_cols = {c["name"] for c in inspector.get_columns("orders")}
    for col_name, col_def in [
        ("payment_method", "VARCHAR(50) DEFAULT 'stripe'"),
        ("crypto_tx_id", "VARCHAR(255) DEFAULT ''"),
        ("crypto_payer", "VARCHAR(255) DEFAULT ''"),
        ("crypto_token", "VARCHAR(50) DEFAULT ''"),
    ]:
        if col_name not in existing_order_cols:
            conn.execute(text(f"ALTER TABLE orders ADD COLUMN {col_name} {col_def}"))
            conn.commit()

# Create upload directory
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs("data", exist_ok=True)

# Configure Stripe
stripe.api_key = STRIPE_SECRET_KEY

app = FastAPI(title="Florida Garage Sales API", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:3000", "https://www.floridagaragesales.com", "https://floridagaragesales.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve uploaded images
if os.path.exists(UPLOAD_DIR):
    app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


# ============ AUTH ============

@app.post("/api/auth/login")
def login(username: str = Form(...), password: str = Form(...)):
    from app.config import ADMIN_USERNAME, ADMIN_PASSWORD
    if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(data={"sub": username})
    return {"access_token": token, "token_type": "bearer"}


# ============ PRODUCTS (PUBLIC) ============

@app.get("/api/products")
def list_products(
    category: Optional[str] = None,
    featured: Optional[bool] = None,
    db: Session = Depends(get_db),
):
    query = db.query(Product).filter(Product.is_active == True)
    if category:
        query = query.filter(Product.category == category)
    if featured:
        query = query.filter(Product.is_featured == True)
    products = query.order_by(Product.created_at.desc()).all()
    return [_product_to_dict(p) for p in products]


@app.get("/api/products/{product_id}")
def get_product(product_id: int, db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return _product_to_dict(product)


# ============ PRODUCTS (ADMIN) ============

@app.post("/api/admin/products")
async def create_product(
    name: str = Form(...),
    description: str = Form(""),
    price: float = Form(...),
    category: str = Form("Other"),
    quantity: int = Form(1),
    condition: str = Form("Used - Good"),
    is_featured: bool = Form(False),
    image: Optional[UploadFile] = File(None),
    admin: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    image_url = ""
    if image and image.filename:
        image_url = await _save_upload(image)

    product = Product(
        name=name,
        description=description,
        price=price,
        category=category,
        quantity=quantity,
        condition=condition,
        is_featured=is_featured,
        image_url=image_url,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return _product_to_dict(product)


@app.put("/api/admin/products/{product_id}")
async def update_product(
    product_id: int,
    name: str = Form(...),
    description: str = Form(""),
    price: float = Form(...),
    category: str = Form("Other"),
    quantity: int = Form(1),
    condition: str = Form("Used - Good"),
    is_featured: bool = Form(False),
    is_active: bool = Form(True),
    image: Optional[UploadFile] = File(None),
    admin: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    product.name = name
    product.description = description
    product.price = price
    product.category = category
    product.quantity = quantity
    product.condition = condition
    product.is_featured = is_featured
    product.is_active = is_active

    if image and image.filename:
        product.image_url = await _save_upload(image)

    db.commit()
    db.refresh(product)
    return _product_to_dict(product)


@app.delete("/api/admin/products/{product_id}")
def delete_product(
    product_id: int,
    admin: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    db.delete(product)
    db.commit()
    return {"detail": "Product deleted"}


@app.get("/api/admin/products")
def admin_list_products(
    admin: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    products = db.query(Product).order_by(Product.created_at.desc()).all()
    return [_product_to_dict(p) for p in products]


# ============ CHECKOUT (STRIPE) ============

@app.post("/api/checkout")
def create_checkout_session(request_data: dict, db: Session = Depends(get_db)):
    items = request_data.get("items", [])
    if not items:
        raise HTTPException(status_code=400, detail="No items provided")

    line_items = []
    for item in items:
        product = db.query(Product).filter(Product.id == item["product_id"]).first()
        if not product or not product.is_active:
            continue
        line_items.append({
            "price_data": {
                "currency": "usd",
                "product_data": {
                    "name": product.name,
                    "description": product.description[:500] if product.description else "",
                    "images": [product.image_url] if product.image_url else [],
                },
                "unit_amount": int(product.price * 100),
            },
            "quantity": item.get("quantity", 1),
        })

    if not line_items:
        raise HTTPException(status_code=400, detail="No valid items")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=line_items,
            mode="payment",
            success_url=f"{FRONTEND_URL}/shop.html?success=true",
            cancel_url=f"{FRONTEND_URL}/shop.html?canceled=true",
            shipping_address_collection={"allowed_countries": ["US"]},
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Create order record
    order = Order(
        stripe_session_id=session.id,
        total_amount=sum(li["price_data"]["unit_amount"] * li["quantity"] for li in line_items) / 100,
        items_json=json.dumps(items),
        status="pending",
    )
    db.add(order)
    db.commit()

    return {"checkout_url": session.url, "session_id": session.id}


@app.post("/api/webhook/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid webhook")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order = db.query(Order).filter(Order.stripe_session_id == session["id"]).first()
        if order:
            order.status = "paid"
            order.customer_email = session.get("customer_details", {}).get("email", "")
            order.customer_name = session.get("customer_details", {}).get("name", "")
            order.stripe_payment_intent = session.get("payment_intent", "")
            shipping = session.get("shipping_details", {})
            if shipping:
                order.shipping_address = json.dumps(shipping)
            db.commit()

            # Send email notification
            threading.Thread(
                target=send_order_notification,
                args=(order.customer_name, order.customer_email, order.total_amount, order.id, order.items_json),
                daemon=True,
            ).start()

    return {"status": "ok"}


# ============ ORDERS (ADMIN) ============

@app.get("/api/admin/orders")
def list_orders(
    status: Optional[str] = None,
    admin: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    query = db.query(Order)
    if status:
        query = query.filter(Order.status == status)
    orders = query.order_by(Order.created_at.desc()).all()
    return [_order_to_dict(o) for o in orders]


@app.put("/api/admin/orders/{order_id}/status")
def update_order_status(
    order_id: int,
    status_data: dict,
    admin: str = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    order.status = status_data.get("status", order.status)
    db.commit()
    return _order_to_dict(order)


# ============ CRYPTO ORDERS ============

@app.post("/api/crypto-order")
def create_crypto_order(request_data: dict, db: Session = Depends(get_db)):
    items = request_data.get("items", [])
    tx_id = request_data.get("tx_id", "")
    payer = request_data.get("payer", "")
    token = request_data.get("token", "")
    amount = request_data.get("amount", 0.0)

    if not items or not tx_id:
        raise HTTPException(status_code=400, detail="Missing items or tx_id")

    order = Order(
        payment_method="crypto",
        crypto_tx_id=tx_id,
        crypto_payer=payer,
        crypto_token=token,
        total_amount=amount,
        items_json=json.dumps(items),
        status="paid",
        customer_name=payer,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return {"order_id": order.id, "status": "paid", "tx_id": tx_id}


# ============ CONFIG ============

@app.get("/api/config")
def get_config():
    return {"stripe_publishable_key": STRIPE_PUBLISHABLE_KEY}


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "Florida Garage Sales API"}


# ============ HELPERS ============

async def _save_upload(file: UploadFile) -> str:
    ext = os.path.splitext(file.filename)[1] if file.filename else ".jpg"
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    content = await file.read()
    with open(filepath, "wb") as f:
        f.write(content)
    return f"/uploads/{filename}"


def _product_to_dict(p: Product) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "price": p.price,
        "category": p.category,
        "image_url": p.image_url,
        "quantity": p.quantity,
        "condition": p.condition,
        "is_active": p.is_active,
        "is_featured": p.is_featured,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _order_to_dict(o: Order) -> dict:
    return {
        "id": o.id,
        "stripe_session_id": o.stripe_session_id or "",
        "payment_method": o.payment_method or "stripe",
        "crypto_tx_id": o.crypto_tx_id or "",
        "crypto_payer": o.crypto_payer or "",
        "crypto_token": o.crypto_token or "",
        "customer_email": o.customer_email,
        "customer_name": o.customer_name,
        "total_amount": o.total_amount,
        "status": o.status,
        "items": json.loads(o.items_json) if o.items_json else [],
        "shipping_address": json.loads(o.shipping_address) if o.shipping_address else None,
        "created_at": o.created_at.isoformat() if o.created_at else None,
    }


def send_order_notification(customer_name: str, customer_email: str, total: float, order_id: int, items_json: str):
    if not SMTP_USER or not SMTP_PASSWORD:
        return

    items = json.loads(items_json) if items_json else []
    items_text = "\n".join(f"  - Product #{item.get('product_id')} x{item.get('quantity', 1)}" for item in items)

    subject = f"New Order #{order_id} - ${total:.2f}"
    body = f"""🎉 New Order Received!

Order #{order_id}
Customer: {customer_name or 'N/A'}
Email: {customer_email or 'N/A'}
Total: ${total:.2f}

Items:
{items_text}

View in admin dashboard: https://www.floridagaragesales.com/admin.html
"""

    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFICATION_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, NOTIFICATION_EMAIL, msg.as_string())
    except Exception:
        pass
