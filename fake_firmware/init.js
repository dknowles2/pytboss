//PitBoss Firmware 0.5.7
//Written by Mauro Minoro for Dansons in 2022-2023

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
load("platform.js");

let isWiFiConnected = false;
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
let lastWasOn = false;
let grillPassword = "";
let wsIsConnecting = false;

RPC.addHandler("PB.GetFirmwareVersion", function (params) {
  return {
    firmwareVersion: "0.5.7",
  };
});

function getCodecTime() {
  return Math.floor(Math.max(Sys.uptime() - 5, 0) / 10);
}

function getCodecKey(key, time) {
  let x = [];
  let l = time;
  while (key.length > 1) {
    let p = l % key.length;
    let v = key[p];
    key.splice(p, 1);
    x.push((v ^ l) & 0xff);
    l = (l * v + v) & 0xff;
  }
  x.push(key[0]);

  return x;
}

function codec(data, key, paddingLen) {
  let out = "";
  let i;
  if (paddingLen > 0) {
    data = chr(0xff) + data;
    for (i = 0; i < paddingLen; i++) {
      let rndv = Math.rand() & 0xff;
      if (rndv === 0xff) {
        rndv = 0xfe;
      }
      data = chr(rndv) + data;
    }
  }

  for (i = 0; i < data.length; i++) {
    let k = key[i % key.length];
    let m = (data.at(i) ^ k) & 0xff;
    out += chr(m);
    let k2 = (i + 1) % key.length;
    if (paddingLen > 0) {
      key[k2] = ((key[k2] ^ m) + i) & 0xff;
    } else {
      key[k2] = ((key[k2] ^ data.at(i)) + i) & 0xff;
    }
  }
  if (paddingLen < 1) {
    for (i = 0; i < out.length; i++) {
      if (out.at(i) === 0xff) {
        out = out.slice(i + 1);
        break;
      }
    }
  }
  return out;
}

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

function fromHexStr(value) {
  let len = value.length;
  let rawData = "";

  //make sure function doesn't crash;
  if ((len & 1) !== 0) {
    value += "0";
  }

  for (let x = 0; x < len; x += 2) {
    let char = (fromHex(value.at(x)) << 4) + fromHex(value.at(x + 1));

    rawData += chr(char);
  }
  return rawData;
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

function handlePacket(pck, isRaw) {
  let decString = "";
  let key = "";
  if (isRaw !== true && pck.length > 1) {
    key = getStatusKey(pck.at(1));
    if (pck.at(1) === 11) {
      if (pck.length > powerStatusPos) {
        moduleIsOn = pck.at(powerStatusPos) === 1;
      }
    }
    if (moduleIsOn === false) {
      vData = null;
    } else {
      if (wsConn === null) {
        WSConnect();
      }
    }
  }
  if (key !== "") {
    lastStatus[key] = pck;
  }
  decString = toHexStr(pck);
  sendBTStatus(decString + " [" + JSON.stringify(decString.length) + "]");
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

        while (true) {
          let dStart = uartBuffer.indexOf("\xFE");
          let iapLen = psUartMessage(uartBuffer, dStart);
          if (iapLen > 0) {
            uartBuffer = uartBuffer.slice(iapLen, uartBuffer.length);
            continue;
          } else if (iapLen < 0) {
            break;
          } else {
            if (dStart >= 0) {
              if (dStart > 0) {
                uartBuffer = uartBuffer.slice(dStart, uartBuffer.length);
              }
              let dEnd = uartBuffer.indexOf("\xFF");
              if (dEnd >= 0) {
                let packet = uartBuffer.slice(0, dEnd + 1);
                uartBuffer = uartBuffer.slice(dEnd + 1, uartBuffer.length);
                if (packet.length > 0) {
                  handlePacket(packet, false);
                }
                continue;
              }
            }
          }
          break;
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

function uartSend(message) {
  UART.write(uartNo, message);
  UART.flush(uartNo);
}

function sendMCUCommand(pCommand) {
  let mcuCommand = fromHexStr(pCommand);

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
  let data = File.read("extconfig.json");
  if (data === null) {
    return;
  }
  let extConfig = JSON.parse(data);
  data = "";
  if (typeof extConfig !== "object") {
    return;
  }
  if (typeof extConfig.psw === "string") {
    grillPassword = codec(
      fromHexStr(extConfig.psw),
      [0xc3, 0x3a, 0x77, 0xf0, 0xda, 0x52, 0x6f, 0x16],
      0
    );
  }
}

function saveExtConfig() {
  let extConfig = {
    psw: toHexStr(
      codec(grillPassword, [0xc3, 0x3a, 0x77, 0xf0, 0xda, 0x52, 0x6f, 0x16], 4)
    ),
  };
  let rawData = JSON.stringify(extConfig);
  let fs = File.fopen("extconfig.json", "w");
  if (fs === null) {
    return;
  }
  File.fwrite(rawData, 1, rawData.length, fs);
  File.fclose(fs);
}

function WSConnect() {
  if (wsIsConnecting === true || isWiFiConnected === false) {
    return;
  }
  wsIsConnecting = true;
  WS.connect({
    url: wsUrl,
    onconnected: function (conn) {
      wsConn = conn;
      wsIsConnecting = false;
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
      print("ondisconnected", wsIsConnecting, wsConn);
      if (wsConn !== null) {
        wsConn = null;
      }
      wsIsConnecting = false;
      //try reconnecting in 5 seconds
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
        if (lastWasOn || moduleIsOn) {
          sendWSStatus();
        }
        lastWasOn = moduleIsOn;
      }
    },
    null
  );
}

function checkPassword(params) {
  if (grillPassword === "") {
    return true;
  }
  if (typeof params !== "object" || typeof params.psw !== "string") {
    return false;
  }
  let x = getCodecTime();

  let key = getCodecKey([0x8f, 0x80, 0x19, 0xcf, 0x77, 0x6c, 0xfe, 0xb7], x);
  let data = fromHexStr(params.psw);
  if (codec(data, key, 0) === grillPassword) {
    return true;
  }
  key = getCodecKey([0x8f, 0x80, 0x19, 0xcf, 0x77, 0x6c, 0xfe, 0xb7], x + 1);
  return codec(data, key, 0) === grillPassword;
}

RPC.addHandler("PB.SendMCUCommand", function (params) {
  if (!checkPassword(params)) {
    return { error: 401, message: "Unauthorized" };
  }
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
      if (isWiFiConnected === true) {
        uartSend("\xFE\x24\x00\xFF");
      }
      isWiFiConnected = false;
    } else if (ev === Net.STATUS_GOT_IP) {
      uartSend("\xFE\x24\x01\xFF");
      isWiFiConnected = true;
      if (moduleIsOn || lastWasOn) {
        WSConnect();
      }
    } else if (ev === Net.STATUS_CONNECTED) {
    }
  },
  null
);

RPC.addHandler("PB.GetState", function (params) {
  if (!checkPassword(params)) {
    return { error: 401, message: "Unauthorized" };
  }
  return {
    sc_11: toHexStr(lastStatus.sc_11),
    sc_12: toHexStr(lastStatus.sc_12),
  };
});

RPC.addHandler("PB.SetWiFiUpdateFrequency", function (params) {
  if (!checkPassword(params)) {
    return { error: 401, message: "Unauthorized" };
  }
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
  if (!checkPassword(params)) {
    return { error: 401, message: "Unauthorized" };
  }
  wsWDT = 5 * 60; //5 minutes timeout
  wsTime = 0;
  return null;
});

RPC.addHandler("PB.SetVirtualData", function (params) {
  if (!checkPassword(params)) {
    return { error: 401, message: "Unauthorized" };
  }
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
  if (!checkPassword(params)) {
    return { error: 401, message: "Unauthorized" };
  }
  if (vData === null) {
    return {};
  }
  return vData;
});

RPC.addHandler("PB.DebugPState", function (params) {
  if (!checkPassword(params)) {
    return { error: 401, message: "Unauthorized" };
  }
  return { pState: pState };
});

RPC.addHandler("PB.SetDevicePassword", function (params) {
  if (!checkPassword(params)) {
    return { error: 401, message: "Unauthorized" };
  }
  if (typeof params === "object" && typeof params.newPassword === "string") {
    grillPassword = codec(
      fromHexStr(params.newPassword),
      [0x8f, 0x80, 0x19, 0xcf, 0x77, 0x6c, 0xfe, 0xb7],
      0
    );
    saveExtConfig();
    return null;
  }
  return { error: -1, message: "Invalid parameters" };
});

RPC.addHandler("PB.SetWifiCredentials", function (params) {
  if (!checkPassword(params)) {
    return { error: 401, message: "Unauthorized" };
  }
  if (
    typeof params === "object" &&
    typeof params.ssid === "string" &&
    typeof params.pass === "string"
  ) {
    let wifiPass = codec(
      fromHexStr(params.pass),
      [0x8f, 0x80, 0x27, 0xcf, 0x41, 0x6c, 0x45, 0xb7],
      0
    );
    Cfg.set(
      {
        wifi: { sta: { ssid: params.ssid, pass: wifiPass, enable: true } },
      },
      false
    );
    return null;
  }
  return { error: -1, message: "Invalid parameters" };
});

RPC.addHandler("PB.GetTime", function (params) {
  return {
    time: Sys.uptime(),
  };
});

RPC.addHandler("PB.RenameDevice", function (params) {
  if (!checkPassword(params)) {
    return { error: 401, message: "Unauthorized" };
  }
  if (typeof params === "object" && typeof params.name === "string") {
    let newName = params.name;
    if (
      newName !== "" &&
      newName[0] !== " " &&
      newName[newName.length - 1] !== " "
    ) {
      let dashPos = deviceId.indexOf("-");
      let prefix = deviceId.slice(0, dashPos + 1);
      Cfg.set(
        {
          device: { id: prefix + newName },
        },
        true
      );
      return { newName: prefix + newName };
    }
  }
  return { error: 400, message: "Invalid parameters" };
});

loadConfig(); //read config

initUART();

initPlatform();
//wait 5 secs before starting the timer task, let things settle
Timer.set(
  5000,
  0,
  function () {
    startMCU_UpdateTimer();
  },
  null
);
