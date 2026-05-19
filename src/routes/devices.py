from fastapi import APIRouter
import usb.core

router = APIRouter()

@router.get("")
async def list_devices():
    devices = []
    for dev in usb.core.find(find_all=True):
        devices.append({
            "vendor_id": hex(dev.idVendor),
            "product_id": hex(dev.idProduct),
        })
    return {"devices": devices}

@router.get("/scan")
async def scan():
    return await list_devices()
