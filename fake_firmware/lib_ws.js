// Dummy placeholder for WebSocket library used by PitBoss firmware.
//
// Also see: https://firmware.dansonscorp.com/lib_ws.js
//
// Since we don't actually use any WebSocket features in our fake
// implementation, we can simply replace all calls with noops.

load("api_log.js");

let WS = {
    connect: function() {
        Log.info("WS.connect()");
    },
    send: function() {
        Log.send("WS.send()");
    }
};
