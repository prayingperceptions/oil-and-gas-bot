import aiohttp
import os
import time
import base64
import logging
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv()

class KalshiClient:
    BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
    
    def __init__(self):
        self.api_key = os.getenv("KALSHI_API_KEY")
        key_path = "kalshi.key"
        
        if not os.path.exists(key_path):
            logger.error(f"Key file {key_path} not found.")
            self.private_key = None
            return

        with open(key_path, "rb") as f:
            self.private_key_pem = f.read()
            
        try:
            self.private_key = load_pem_private_key(self.private_key_pem, password=None)
        except Exception as e:
            logger.error(f"Failed to load RSA key: {e}")
            self.private_key = None
        
    def _generate_headers(self, method: str, path: str):
        if not self.private_key:
            return {}
            
        timestamp = int(time.time() * 1000)
        msg_string = f"{timestamp}{method}{path}"
        
        signature = self.private_key.sign(
            msg_string.encode('utf-8'),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        
        signature_b64 = base64.b64encode(signature).decode('utf-8')
        
        return {
            "KALSHI-ACCESS-KEY": self.api_key or "",
            "KALSHI-ACCESS-SIGNATURE": signature_b64,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp),
            "Content-Type": "application/json"
        }

    async def get_balance(self):
        path = "/portfolio/balance"
        headers = self._generate_headers("GET", path)
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.BASE_URL}{path}", headers=headers) as response:
                return await response.json()
                
    async def get_market(self, ticker: str):
        path = f"/markets/{ticker}"
        headers = self._generate_headers("GET", path)
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.BASE_URL}{path}", headers=headers) as response:
                return await response.json()

    async def create_order(self, ticker: str, action: str, type: str, yes_price: int, count: int):
        path = "/portfolio/orders"
        headers = self._generate_headers("POST", path)
        payload = {
            "action": action, # 'buy' or 'sell'
            "client_order_id": str(int(time.time() * 1000)),
            "count": count,
            "side": "yes", # default to working with yes contracts
            "ticker": ticker,
            "type": type, # 'market' or 'limit'
            "yes_price": yes_price
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.BASE_URL}{path}", json=payload, headers=headers) as response:
                return await response.json()
