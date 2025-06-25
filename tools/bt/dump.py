import asyncio
from bleak import BleakClient

# Your remote’s MAC and the two vendor-specific char UUIDs
ADDRESS     = "AA:BB:CC:DD:EE:FF"
CHAR_UUIDS  = [
    "a15616b1-04ba-11e5-b939-0800200c9a66",
    "a15616b2-04ba-11e5-b939-0800200c9a66",
]

async def handle_data(sender, data: bytearray):
    print(f"[{sender}] {data.hex()}")

async def run():
    while True:
        try:
            async with BleakClient(ADDRESS, timeout=10.0) as client:
                print(f"✅ Connected to {ADDRESS}")
                # Subscribe to the two known characteristics
                for uuid in CHAR_UUIDS:
                    print(f"→ Subscribing to {uuid}")
                    await client.start_notify(uuid, handle_data)
                # Keep the loop alive while notifications arrive
                while client.is_connected:
                    await asyncio.sleep(1)
        except Exception as e:
            print("🔌 Disconnected or error:", e)
        print("⏳ Reconnecting in 5s…")
        await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(run())
