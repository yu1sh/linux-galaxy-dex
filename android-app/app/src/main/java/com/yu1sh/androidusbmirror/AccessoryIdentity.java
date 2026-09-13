package com.yu1sh.androidusbmirror;

import android.hardware.usb.UsbAccessory;

/** The generic AOA identity used by the desktop-side libusb host. */
final class AccessoryIdentity {
    static final String MANUFACTURER = "Omarchy";
    static final String MODEL = "Galaxy USB MediaProjection";

    private AccessoryIdentity() {
    }

    static boolean matches(UsbAccessory accessory) {
        return accessory != null
                && MANUFACTURER.equals(accessory.getManufacturer())
                && MODEL.equals(accessory.getModel());
    }

    static String describe(UsbAccessory accessory) {
        if (accessory == null) {
            return "No USB accessory";
        }
        String manufacturer = valueOrUnknown(accessory.getManufacturer());
        String model = valueOrUnknown(accessory.getModel());
        return manufacturer + " / " + model;
    }

    private static String valueOrUnknown(String value) {
        return value == null || value.isEmpty() ? "unknown" : value;
    }
}
