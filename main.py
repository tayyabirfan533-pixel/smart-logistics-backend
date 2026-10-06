from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from pydantic import BaseModel
from datetime import datetime
import os, math

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./logistics.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

app = FastAPI(title="Smart Logistics & Delivery Tracking API", version="1.0.0")


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    role = Column(String, default="customer")


class Driver(Base):
    __tablename__ = "drivers"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    status = Column(String, default="available")


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("users.id"))
    driver_id = Column(Integer, ForeignKey("drivers.id"), nullable=True)
    pickup_lat = Column(Float, nullable=False)
    pickup_lng = Column(Float, nullable=False)
    delivery_lat = Column(Float, nullable=False)
    delivery_lng = Column(Float, nullable=False)
    status = Column(String, default="created")
    eta_minutes = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


class UserCreate(BaseModel):
    name: str
    email: str
    role: str = "customer"


class DriverCreate(BaseModel):
    user_id: int
    latitude: float
    longitude: float


class LocationUpdate(BaseModel):
    latitude: float
    longitude: float


class OrderCreate(BaseModel):
    customer_id: int
    pickup_lat: float
    pickup_lng: float
    delivery_lat: float
    delivery_lng: float


class StatusUpdate(BaseModel):
    status: str


connections = {}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def distance(lat1, lng1, lat2, lng2):
    return math.sqrt((lat1 - lat2) ** 2 + (lng1 - lng2) ** 2)


async def notify_user(user_id, message):
    ws = connections.get(user_id)
    if ws:
        try:
            await ws.send_json({"type": "notification", "message": message})
        except Exception:
            pass


@app.get("/")
def root():
    return {"message": "Smart Logistics & Delivery Tracking API", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/users")
def create_user(data: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(400, "Email already exists")

    user = User(name=data.name, email=data.email, role=data.role)
    db.add(user)
    db.commit()
    db.refresh(user)

    return {"id": user.id, "name": user.name, "email": user.email, "role": user.role}


@app.post("/api/drivers")
def create_driver(data: DriverCreate, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == data.user_id).first()

    if not user:
        raise HTTPException(404, "User not found")

    driver = Driver(
        user_id=data.user_id,
        latitude=data.latitude,
        longitude=data.longitude,
        status="available"
    )
    db.add(driver)
    db.commit()
    db.refresh(driver)

    return {
        "id": driver.id,
        "user_id": driver.user_id,
        "status": driver.status,
        "latitude": driver.latitude,
        "longitude": driver.longitude
    }


@app.get("/api/drivers")
def get_drivers(db: Session = Depends(get_db)):
    drivers = db.query(Driver).all()

    return [
        {
            "id": d.id,
            "user_id": d.user_id,
            "latitude": d.latitude,
            "longitude": d.longitude,
            "status": d.status
        }
        for d in drivers
    ]


@app.patch("/api/drivers/{driver_id}/location")
async def update_driver_location(
    driver_id: int,
    data: LocationUpdate,
    db: Session = Depends(get_db)
):
    driver = db.query(Driver).filter(Driver.id == driver_id).first()

    if not driver:
        raise HTTPException(404, "Driver not found")

    driver.latitude = data.latitude
    driver.longitude = data.longitude
    db.commit()

    active_order = db.query(Order).filter(
        Order.driver_id == driver_id,
        Order.status.in_(["assigned", "picked_up", "in_transit"])
    ).first()

    if active_order:
        await notify_user(
            active_order.customer_id,
            {
                "order_id": active_order.id,
                "latitude": driver.latitude,
                "longitude": driver.longitude
            }.__str__()
        )

    return {"message": "Location updated"}


@app.post("/api/orders")
async def create_order(data: OrderCreate, db: Session = Depends(get_db)):
    customer = db.query(User).filter(User.id == data.customer_id).first()

    if not customer:
        raise HTTPException(404, "Customer not found")

    order = Order(
        customer_id=data.customer_id,
        pickup_lat=data.pickup_lat,
        pickup_lng=data.pickup_lng,
        delivery_lat=data.delivery_lat,
        delivery_lng=data.delivery_lng,
        status="searching_driver"
    )

    db.add(order)
    db.commit()
    db.refresh(order)

    available_drivers = db.query(Driver).filter(
        Driver.status == "available"
    ).all()

    if not available_drivers:
        order.status = "created"
        db.commit()
        return {
            "order_id": order.id,
            "status": "created",
            "message": "No driver available"
        }

    nearest = min(
        available_drivers,
        key=lambda d: distance(
            d.latitude,
            d.longitude,
            order.pickup_lat,
            order.pickup_lng
        )
    )

    nearest_distance = distance(
        nearest.latitude,
        nearest.longitude,
        order.pickup_lat,
        order.pickup_lng
    )

    order.driver_id = nearest.id
    order.status = "assigned"
    order.eta_minutes = max(1, round(nearest_distance * 60))
    nearest.status = "assigned"

    db.commit()

    await notify_user(
        nearest.user_id,
        f"New delivery request: Order #{order.id}"
    )

    return {
        "order_id": order.id,
        "driver_id": nearest.id,
        "status": order.status,
        "eta_minutes": order.eta_minutes
    }


@app.post("/api/orders/{order_id}/accept")
async def accept_order(order_id: int, driver_id: int, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    driver = db.query(Driver).filter(Driver.id == driver_id).first()

    if not order or not driver:
        raise HTTPException(404, "Order or driver not found")

    if order.driver_id != driver_id:
        raise HTTPException(400, "Order is not assigned to this driver")

    if driver.status != "assigned":
        raise HTTPException(400, "Driver is not assigned")

    order.status = "assigned"
    db.commit()

    await notify_user(order.customer_id, f"Driver assigned to Order #{order.id}")

    return {"order_id": order.id, "status": order.status}


@app.post("/api/orders/{order_id}/reject")
async def reject_order(order_id: int, driver_id: int, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    driver = db.query(Driver).filter(Driver.id == driver_id).first()

    if not order or not driver:
        raise HTTPException(404, "Order or driver not found")

    driver.status = "available"
    order.driver_id = None
    order.status = "searching_driver"

    available = db.query(Driver).filter(
        Driver.status == "available",
        Driver.id != driver_id
    ).all()

    if available:
        nearest = min(
            available,
            key=lambda d: distance(
                d.latitude,
                d.longitude,
                order.pickup_lat,
                order.pickup_lng
            )
        )

        order.driver_id = nearest.id
        order.status = "assigned"
        order.eta_minutes = max(
            1,
            round(
                distance(
                    nearest.latitude,
                    nearest.longitude,
                    order.pickup_lat,
                    order.pickup_lng
                ) * 60
            )
        )
        nearest.status = "assigned"

        db.commit()

        await notify_user(
            nearest.user_id,
            f"New delivery request: Order #{order.id}"
        )

        return {
            "order_id": order.id,
            "driver_id": nearest.id,
            "status": "assigned"
        }

    db.commit()

    return {
        "order_id": order.id,
        "status": "searching_driver",
        "message": "No other driver available"
    }


@app.patch("/api/orders/{order_id}/status")
async def update_order_status(
    order_id: int,
    data: StatusUpdate,
    db: Session = Depends(get_db)
):
    allowed = {
        "created",
        "searching_driver",
        "assigned",
        "picked_up",
        "in_transit",
        "delivered",
        "cancelled"
    }

    if data.status not in allowed:
        raise HTTPException(400, "Invalid delivery status")

    order = db.query(Order).filter(Order.id == order_id).first()

    if not order:
        raise HTTPException(404, "Order not found")

    order.status = data.status

    if data.status == "delivered" and order.driver_id:
        driver = db.query(Driver).filter(
            Driver.id == order.driver_id
        ).first()

        if driver:
            driver.status = "available"

    db.commit()

    await notify_user(
        order.customer_id,
        f"Order #{order.id} status changed to {order.status}"
    )

    return {"order_id": order.id, "status": order.status}


@app.get("/api/orders")
def get_orders(db: Session = Depends(get_db)):
    orders = db.query(Order).order_by(Order.id.desc()).all()

    return [
        {
            "id": o.id,
            "customer_id": o.customer_id,
            "driver_id": o.driver_id,
            "pickup": {
                "latitude": o.pickup_lat,
                "longitude": o.pickup_lng
            },
            "delivery": {
                "latitude": o.delivery_lat,
                "longitude": o.delivery_lng
            },
            "status": o.status,
            "eta_minutes": o.eta_minutes,
            "created_at": o.created_at
        }
        for o in orders
    ]


@app.get("/api/orders/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()

    if not order:
        raise HTTPException(404, "Order not found")

    return {
        "id": order.id,
        "customer_id": order.customer_id,
        "driver_id": order.driver_id,
        "status": order.status,
        "eta_minutes": order.eta_minutes,
        "pickup": [order.pickup_lat, order.pickup_lng],
        "delivery": [order.delivery_lat, order.delivery_lng]
    }


@app.get("/api/history/{customer_id}")
def delivery_history(customer_id: int, db: Session = Depends(get_db)):
    orders = db.query(Order).filter(
        Order.customer_id == customer_id,
        Order.status.in_(["delivered", "cancelled"])
    ).order_by(Order.id.desc()).all()

    return [
        {
            "order_id": o.id,
            "driver_id": o.driver_id,
            "status": o.status,
            "eta_minutes": o.eta_minutes,
            "created_at": o.created_at
        }
        for o in orders
    ]


@app.get("/api/admin/analytics")
def analytics(db: Session = Depends(get_db)):
    total_orders = db.query(Order).count()
    delivered = db.query(Order).filter(Order.status == "delivered").count()
    active = db.query(Order).filter(
        Order.status.in_([
            "searching_driver",
            "assigned",
            "picked_up",
            "in_transit"
        ])
    ).count()
    available_drivers = db.query(Driver).filter(
        Driver.status == "available"
    ).count()

    return {
        "total_orders": total_orders,
        "delivered_orders": delivered,
        "active_orders": active,
        "available_drivers": available_drivers
    }


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await websocket.accept()
    connections[user_id] = websocket

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if connections.get(user_id) == websocket:
            del connections[user_id]
