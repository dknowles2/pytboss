// Fake UART API.
//
// This roughly simulates the behavior of the PitBoss UART module.

load("api_log.js");
load("api_timer.js");

let UART = {
    _dispatchers: {},
    _ra: {},
    _data: {},
    _status: {
        p_1_Set_Temp: "\x01\x06\x05",
        p_1_Temp: "\x01\x09\x01",
        p_2_Temp: "\x01\x09\x02",
        p_3_Temp: "\x09\x06\x00",
        p_4_Temp: "\x09\x06\x00",
        smokerActTemp: "\x02\x02\x00",
        grillSetTemp: "\x02\x02\x05",
        grillTemp: "\x02\x02\x00",
        moduleIsOn: "\x01",
        err_1: "\x00",
        err_2: "\x00",
        err_3: "\x00",
        tempHighErr: "\x00",
        fanErr: "\x00",
        hotErr: "\x00",
        motorErr: "\x00",
        noPellets: "\x00",
        erL: "\x00",
        fanState: "\x01",
        hotState: "\x01",
        motorState: "\x01",
        lightState: "\x00",
        primeState: "\x01",
        isFahrenheit: "\x01",
        recipeStep: "\x04",
        time_H: "\x0C",
        time_M: "\x3B",
        time_S: "\x1F",
    },

    setConfig: function(uartNo, param) {},
    setRxEnabled: function(uartNo) {},
    setDispatcher: function(uartNo, callback, userdata) {
        this._ra[uartNo] = 0;
        this._data[uartNo] = "";
        this._dispatchers[uartNo] = callback;
    },
    flush: function(uartNo) {
        if (this._dispatchers[uartNo]) {
            this._dispatchers[uartNo](uartNo);
        }
    },
    write: function(uartNo, data) {
        this._data[uartNo] = "";
        if (data === "\xFE\x0c\x01\xFF") {
            // get-temperatures
            let s = this._status;
            this._data[uartNo] = (
                "\xFE\x0B" +
                s.p_1_Set_Temp +
                s.p_1_Temp +
                s.p_2_Temp +
                s.p_3_Temp +
                s.p_4_Temp +
                s.smokerActTemp +
                s.grillSetTemp +
                "\x01" +  // condGrillTemp
                s.moduleIsOn +
                s.err_1 +
                s.err_2 +
                s.err_3 +
                s.tempHighErr +
                s.fanErr +
                s.hotErr +
                s.motorErr +
                s.noPellets +
                s.erL +
                s.fanState +
                s.hotState +
                s.motorState +
                s.lightState +
                s.primeState +
                s.isFahrenheit +
                s.recipeStep +
                s.time_H +
                s.time_M +
                s.time_S +
                "\xFF");
        } else if (data === "\xFE\x0B\x01\xFF") {
            // get-status
            let s = this._status;
            this._data[uartNo] = (
                "\xFE\x0C" +
                s.p_1_Set_Temp +
                s.p_1_Temp +
                s.p_2_Temp +
                s.p_3_Temp +
                s.p_4_Temp +
                s.smokerActTemp +
                s.grillSetTemp +
                s.grillTemp +
                s.isFahrenheit +
                "\xFF");
        } else if (data.indexOf("\xFE\x05\x01") === 0) {
            // set-grill-temperature
            this._status.grillSetTemp = data.slice(3, 6);
            let s = this._status;
            this._data[uartNo] = (
                "\xFE\x0D" +
                s.grillSetTemp +
                s.p_1_Set_Temp +
                "\xFF");
            Timer.set(100, 0, function() { UART.flush(uartNo); }, null);
        } else if (data.indexOf("\xFE\x05\x02") === 0) {
            // set-probe-1-temperature
            this._status.p_1_Set_Temp = data.slice(3, 6);
            let s = this._status;
            this._data[uartNo] = (
                "\xFE\x0D" +
                s.grillSetTemp +
                s.p_1_Set_Temp +
                "\xFF");
            Timer.set(100, 0, function() { UART.flush(uartNo); }, null);
        } else if (data === "\xFE\x09\x01\xFF") {
            // set-fahrenheit
            this._status.isFahrenheit = true;
            // TODO: Convert all temps to fahrenheit.
            // TODO: Does this return anything?
        } else if (data === "\xFE\x09\x02\xFF") {
            // set-celsius
            this._status.isFahrenheit = false;
            // TODO: Convert all temps to celsius.
            // TODO: Does this return anything?
        } else if (data === "\xFE\x02\x02\xFF") {
            // turn-light-off
            this._status.lightState = false;
            // TODO: Does this return anything?
        } else if (data === "\xFE\x02\x01\xFF") {
            // turn-light-on
            this._status.lightState = true;
            // TODO: Does this return anything?
        } else if (data === "\xFE\x01\x02\xFF") {
            // turn-off
            this._status.moduleIsOn = false;
            // TODO: Should this do anything else?
            // TODO: Does this return anything?
        } else if (data === "\xFE\x80\x00\xFF") {
            // turn-primer-motor-off
            this._status.primeState = false;
            // TODO: Does this return anything?
        } else if (data === "\xFE\x80\x01\xFF") {
            // turn-primer-motor-on
            this._status.primeState = true;
            // TODO: Does this return anything?
        } else {
            Log.warn("Ignoring unknown command:" + JSON.stringify(data));
        }
        this._ra[uartNo] = this._data[uartNo].length;
    },
    readAvail: function(uartNo) {
        return this._ra[uartNo];
    },
    read: function(uartNo) {
        let data = this._data[uartNo];
        this._data[uartNo] = "";
        this._ra[uartNo] = 0;
        return data;
    },
};
