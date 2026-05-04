import os
import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
from jose import JWTError, jwt
from passlib.context import CryptContext
import models, database

load_dotenv()

models.Base.metadata.create_all(bind=database.engine)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Security Config ---
SECRET_KEY = "trading-journal-secret-key-2024"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

# --- Schemas ---
class TradeCreate(BaseModel):
    pair: str
    direction: str
    entry_price: float
    exit_price: float
    lot_size: float
    pnl: float
    session: Optional[str] = None
    emotion: Optional[str] = None
    notes: Optional[str] = None

class UserCreate(BaseModel):
    username: str
    password: str

class TokenData(BaseModel):
    username: Optional[str] = None

# --- Auth Helpers ---
def hash_password(password: str):
    return pwd_context.hash(password)

def verify_password(plain, hashed):
    return pwd_context.verify(plain, hashed)

def create_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(database.get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(models.User).filter(models.User.username == username).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user

# --- Auth Routes ---
@app.get("/")
def root():
    return {"status": "Trading Journal API is live 🚀"}

@app.post("/auth/register")
def register(user: UserCreate, db: Session = Depends(database.get_db)):
    existing = db.query(models.User).filter(models.User.username == user.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already taken")
    new_user = models.User(
        username=user.username,
        hashed_password=hash_password(user.password)
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    token = create_token({"sub": new_user.username})
    return {"access_token": token, "token_type": "bearer"}

@app.post("/auth/login")
def login(user: UserCreate, db: Session = Depends(database.get_db)):
    db_user = db.query(models.User).filter(models.User.username == user.username).first()
    if not db_user or not verify_password(user.password, db_user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token({"sub": db_user.username})
    return {"access_token": token, "token_type": "bearer"}

# --- Trade Routes ---
@app.post("/trades")
def create_trade(trade: TradeCreate, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    db_trade = models.Trade(**trade.dict(), user_id=current_user.id)
    db.add(db_trade)
    db.commit()
    db.refresh(db_trade)
    return db_trade

@app.get("/trades")
def get_trades(db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    return db.query(models.Trade).filter(models.Trade.user_id == current_user.id).order_by(models.Trade.date.desc()).all()

@app.delete("/trades/{trade_id}")
def delete_trade(trade_id: int, db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    trade = db.query(models.Trade).filter(models.Trade.id == trade_id, models.Trade.user_id == current_user.id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    db.delete(trade)
    db.commit()
    return {"message": "Trade deleted"}

@app.post("/analysis")
def get_analysis(db: Session = Depends(database.get_db), current_user: models.User = Depends(get_current_user)):
    trades = db.query(models.Trade).filter(models.Trade.user_id == current_user.id).order_by(models.Trade.date).all()

    if not trades:
        raise HTTPException(status_code=400, detail="No trades to analyze")

    trade_data = [
        {
            "pair": t.pair,
            "direction": t.direction,
            "entry": t.entry_price,
            "exit": t.exit_price,
            "lots": t.lot_size,
            "pnl": t.pnl,
            "session": t.session,
            "emotion": t.emotion,
            "notes": t.notes,
            "date": str(t.date)
        }
        for t in trades
    ]

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    prompt = f"""You are an expert trading coach and behavioral analyst.
A trader has shared their complete trade history with you.
Analyze this data and provide a detailed behavioral assessment.

Trade Data:
{trade_data}

Please analyze and provide:
1. PERFORMANCE SUMMARY - overall P&L, win rate, best and worst pairs
2. BEHAVIORAL PATTERNS - revenge trading signs, emotional trading, overtrading
3. SESSION ANALYSIS - which sessions they perform best and worst in
4. PAIR ANALYSIS - which pairs are profitable vs losing
5. KEY RECOMMENDATIONS - 3 specific, actionable things they should change

Be direct, specific, and reference actual numbers from their data.
Address the trader directly as "you".
Do NOT use markdown formatting, hashtags, asterisks, or dashes. Write in plain text only."""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )

    return {"analysis": message.content[0].text}