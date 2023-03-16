//PitBoss Firmware 0.2.3
//Written by Mauro Minoro for Dansons in 2022

load("api_ota.js");
load("api_events.js");
load("api_config.js");
load("api_sys.js");
load("api_net.js");
load("api_timer.js");
load("api_uart.js");
load("api_file.js");
load("api_wifi.js");
load("api_esp32.js");
load("api_rpc.js");
load("api_gpio.js");
load("api_bt_gattc.js");
load("lib_ws.js");

let hasDisconnected = false;
let uartNo = 1;
let uartBuffer = "";
let lastStatus = {
  sc_11: "",
  sc_12: "",
};
let MCU_UpdateInterval = 2;

let MCUTimer;
let MCUTimerActive = false;
let grillUpdateFlag = false;

let wsConn = null;
let deviceId = Cfg.get("device.id");
let wsUrl = "https://socket.dansonscorp.com/from/" + deviceId;
let pState = "";
let wsWDT = 0;
let wsFastInterval = 5; //fast update speed
let wsSlowInterval = 60; //slow update speed
let wsTime = 0;
let mcuTime = 0;
let vData = null;
let sendVData = false;
let moduleIsOn = false;
let powerStatusPos = {
  PBL: 24,
  PBC: 24,
  PBG: 24,
  PBM: 18,
  LFS: 21,
  LBL: 21,
  PBA: 27,
  PBB: 23,
  PBV: 24,
};

function toHex(num) {
  let digits = "0123456789ABCDEF";
  let lo = num & 0x0f;
  let hi = (num >> 4) & 0x0f;

  let hexString = digits[hi] + digits[lo];

  return hexString;
}

function fromHex(hex) {
  if (hex >= 48 && hex <= 57) {
    return hex - 48;
  }
  if (hex >= 65 && hex <= 70) {
    return hex - 55;
  }
  if (hex >= 97 && hex <= 102) {
    return hex - 87;
  }
  return 0;
}

function toHexStr(raw) {
  let result = "";
  for (let i = 0; i < raw.length; i++) {
    result += toHex(raw.at(i));
  }
  return result;
}

function getStatusKey(commandNumber) {
  if (commandNumber >= 11 && commandNumber <= 12) {
    return "sc_" + JSON.stringify(commandNumber);
  }
  return "";
}

function sendBTStatus(status) {
  print("<==PB: ", status);
}

function sendBTData(data) {
  if (data === null) {
    return;
  }
  print("<==PBD: ", JSON.stringify(data));
}

function sendWSStatus() {
  if (wsConn !== null) {
    let wsStatus = [];
    if (lastStatus.sc_11 !== "") {
      wsStatus.push(toHexStr(lastStatus.sc_11));
    }
    if (lastStatus.sc_12 !== "") {
      wsStatus.push(toHexStr(lastStatus.sc_12));
    }
    if (wsStatus.length < 1) {
      return;
    }

    let data = {
      id: -1,
      src: deviceId,
      status: wsStatus,
    };
    if (sendVData === true && vData !== null) {
      data.data = vData;
    }
    if (pState !== "") {
      data.pState = pState;
    }
    let toSend = JSON.stringify(data);
    WS.send(wsConn, toSend);
    sendVData = false;
  }
}

function handlePacket(pck) {
  let decString = "";
  let key = getStatusKey(pck.at(1));
  if (pck.at(1) === 11) {
    let deviceType = deviceId.slice(0, 3);
    if (typeof powerStatusPos[deviceType] === "number") {
      let pos = powerStatusPos[deviceType];
      if (pck.length > pos) {
        moduleIsOn = pck.at(pos) === 1;
      }
    }
  }
  if (moduleIsOn === false) {
    vData = null;
  }
  if (key !== "") {
    lastStatus[key] = pck;
    decString = toHexStr(pck);
    sendBTStatus(decString + " [" + JSON.stringify(decString.length) + "]");
  } else {
    decString = toHexStr(pck);
    sendBTStatus(decString + " [" + JSON.stringify(decString.length) + "]");
  }
}

function initUART() {
  UART.setConfig(uartNo, {
    baudRate: 115200,
    rxBufSize: 1024,
    txBufSize: 1024,

    numDataBits: 8,
    parity: 0,
    numStopBits: 1,

    esp32: {
      gpio: {
        rx: 16,
        tx: 17,
      },
    },
  });

  UART.setRxEnabled(uartNo, true);

  UART.setDispatcher(
    uartNo,
    function (uartNo) {
      let ra = UART.readAvail(uartNo);

      if (ra > 0) {
        let data = UART.read(uartNo);

        uartBuffer += data;
        let dStart = uartBuffer.indexOf("\xFE");
        if (dStart >= 0) {
          if (dStart > 0) {
            uartBuffer = uartBuffer.slice(dStart, uartBuffer.length);
          }
          let dEnd = uartBuffer.indexOf("\xFF");
          if (dEnd >= 0) {
            let packet = uartBuffer.slice(0, dEnd + 1);
            uartBuffer = uartBuffer.slice(dEnd + 1, uartBuffer.length);
            handlePacket(packet);
          }
        }
        if (uartBuffer.length > 2048) {
          uartBuffer = uartBuffer.slice(
            uartBuffer.length - 2048,
            uartBuffer.length
          );
        }
      }
    },
    null
  );
}

function rebootWithDelay(Delay) {
  Timer.set(
    Delay,
    0,
    function () {
      Sys.reboot(0);
    },
    null
  );
}

function setWiFiCredentials(pSSID, pPASSWORD) {
  Cfg.set({ wifi: { sta: { ssid: pSSID, pass: pPASSWORD, enable: true } } });

  rebootWithDelay(2000);
}

function uartSend(message) {
  UART.write(uartNo, message);
  UART.flush(uartNo);
}

function sendMCUCommand(pCommand) {
  let pRawCommand = pCommand + " ";
  let numberOfDigits = pRawCommand.length >> 1;
  let mcuCommand = "";

  for (let x = 0; x < numberOfDigits; x++) {
    let position = x << 1;
    let char =
      (fromHex(pRawCommand.at(position)) << 4) +
      fromHex(pRawCommand.at(position + 1));

    mcuCommand = mcuCommand + chr(char);
  }
  lastStatus.sc_11 = "";
  lastStatus.sc_12 = "";
  uartSend(mcuCommand);
}

function saveConfig() {
  Cfg.set({
    app: {
      wsFastInterval: wsFastInterval,
      wsSlowInterval: wsSlowInterval,
    },
  });
}

function loadConfig() {
  if (typeof Cfg.get("app.wsFastInterval") === "number") {
    wsFastInterval = Cfg.get("app.wsFastInterval");
  }
  if (typeof Cfg.get("app.wsSlowInterval") === "number") {
    wsSlowInterval = Cfg.get("app.wsSlowInterval");
  }
}

function WSConnect() {
  WS.connect({
    url: wsUrl,
    onconnected: function (conn) {
      wsConn = conn;
    },
    onframe: function (conn, data, op) {
      let rpccmd = JSON.parse(data);
      if (typeof rpccmd === "object" && typeof rpccmd.setPState === "string") {
        pState = rpccmd.setPState;
        return;
      }
      if (
        typeof rpccmd === "object" &&
        typeof rpccmd.id === "number" &&
        typeof rpccmd.method === "string"
      ) {
        RPC.call(
          RPC.LOCAL,
          rpccmd.method,
          rpccmd.params,
          function (resp, err_code, err_msg, ud) {
            let rpcResp = { id: ud.id, src: deviceId };
            if (typeof ud.app_id === "string") {
              rpcResp.app_id = ud.app_id;
            }
            if (err_code !== 0) {
              rpcResp.error = { code: err_code, message: err_msg };
            } else {
              rpcResp.result = resp;
            }
            let toSend = JSON.stringify(rpcResp);
            WS.send(wsConn, toSend);
          },
          rpccmd
        );
      } else {
        let toSend2 = JSON.stringify({
          src: deviceId,
          error: { code: -1000, message: "invalid data: " + data },
        });
        WS.send(conn, toSend2);
      }
    },
    ondisconnected: function (err) {
      if (wsConn !== null) {
        wsConn = null;
      }
      //try reconnecting in 5 seconds
      Timer.set(
        5000,
        0,
        function () {
          WSConnect();
        },
        null
      );
    },
  });
}

function setMCUUpdateFrequency(freq) {
  MCU_UpdateInterval = freq;
}

function stopMCU_UpdateTimer() {
  if (MCUTimerActive) {
    Timer.del(MCUTimer);
    MCUTimerActive = false;
  }
}

function startMCU_UpdateTimer() {
  stopMCU_UpdateTimer();

  MCUTimerActive = true;

  MCUTimer = Timer.set(
    1000,
    Timer.REPEAT,
    function () {
      if (MCU_UpdateInterval <= 0) {
        return;
      }
      mcuTime--;
      if (mcuTime <= 0) {
        mcuTime = MCU_UpdateInterval;
        if (grillUpdateFlag) {
          uartSend("\xFE\x0C\x01\xFF");
        } else {
          uartSend("\xFE\x0B\x01\xFF");
        }
        grillUpdateFlag = !grillUpdateFlag;
      }

      if (wsWDT > 0) {
        wsWDT--;
      }
      wsTime--;
      if (wsTime <= 0) {
        wsTime = wsWDT > 0 ? wsFastInterval : wsSlowInterval;
        sendWSStatus();
      }
    },
    null
  );
}

RPC.addHandler("PB.SendMCUCommand", function (params) {
  if (typeof params === "object") {
    if (typeof params.command === "string") {
      if (params.command.length > 0) {
        sendMCUCommand(params.command);

        return null;
      } else {
        return { error: -1, message: "Empty command" };
      }
    } else {
      return { error: -1, message: "Command parameter missing" };
    }
  } else {
    return { error: -1, message: "Invalid parameters" };
  }
});

Event.addGroupHandler(
  Net.EVENT_GRP,
  function (ev, evdata, arg) {
    if (ev === Net.STATUS_DISCONNECTED) {
      if (hasDisconnected === false) {
        currentIP = "";
        uartSend("\xFE\x24\x00\xFF");
      }
      hasDisconnected = true;
    } else if (ev === Net.STATUS_GOT_IP) {
      WSConnect();
    } else if (ev === Net.STATUS_CONNECTED) {
      uartSend("\xFE\x24\x01\xFF");
      hasDisconnected = false;
    }
  },
  null
);

RPC.addHandler("PB.GetState", function (params) {
  return {
    sc_11: toHexStr(lastStatus.sc_11),
    sc_12: toHexStr(lastStatus.sc_12),
  };
});

RPC.addHandler("PB.SetWiFiUpdateFrequency", function (params) {
  if (
    typeof params === "object" &&
    typeof params.slow === "number" &&
    typeof params.fast === "number"
  ) {
    wsFastInterval = params.fast;
    wsSlowInterval = params.slow;
    if (typeof params.save === "boolean") {
      if (params.save === true) {
        saveConfig();
      }
    }
    return null;
  }
  return { error: -1, message: "Invalid parameters" };
});

RPC.addHandler("PB.SetMCU_UpdateFrequency", function (params) {
  if (typeof params === "object" && typeof params.frequency === "number") {
    MCU_UpdateInterval = params.frequency;
    return null;
  }
  return { error: -1, message: "Invalid parameters" };
});

RPC.addHandler("PB.WiFiAwakeWDT", function (params) {
  wsWDT = 5 * 60; //5 minutes timeout
  wsTime = 0;
  return null;
});

RPC.addHandler("PB.GetFirmwareVersion", function (params) {
  return {
    firmwareVersion: "0.2.3",
  };
});

RPC.addHandler("PB.SetVirtualData", function (params) {
  if (!moduleIsOn) {
    return { error: -1, message: "Grill is Off" };
  }
  if (typeof params === "object") {
    vData = params;
    wsTime = 1;
    sendVData = true;
    sendBTData(vData);
  }
});

RPC.addHandler("PB.GetVirtualData", function (params) {
  if (vData === null) {
    return {};
  }
  return vData;
});

RPC.addHandler("PB.DebugPState", function (params) {
  return { pState: pState };
});

loadConfig(); //read config

initUART();

//wait 5 secs before starting the timer task, let things settle
Timer.set(
  5000,
  0,
  function () {
    startMCU_UpdateTimer();
  },
  null
);
