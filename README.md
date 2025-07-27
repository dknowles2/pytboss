# pytboss

Python 3 library for interacting with Pitboss grills and smokers.

*Note that this project has no official relationship with Pitboss or Danson's. Use at your own risk.*

## Usage

```python
import asyncio
from bleak import BleakScanner
from pytboss import BleConnection, PitBoss


async def state_callback(data):
    print(data)


async def main():
    ble_device = await BleakScanner.find_device_by_address(device_address)
    model = "PBV4PS2"  # Or your model. See below.
    boss = PitBoss(BleConnection(ble_device), model)
    # Subscribe to updates from the smoker.
    await boss.subscribe_state(state_callback)
    await boss.start()
    while True:
        asyncio.sleep(0.1)


asyncio.run(main())
```

## Installation

### Pip

To install pytboss, run this command in your terminal:

```sh
$ pip install pytboss
```

### Source code

Pytboss is actively developed on Github, where the code is [always available](https://github.com/dknowles2/pytboss).

You can either clone the public repository:

```sh
$ git clone https://github.com/dknowles2/pytboss
```

Or download the latest [tarball](https://github.com/dknowles2/pytboss/tarball/main):

```sh
$ curl -OL https://github.com/dknowles2/pytboss/tarball/main
```

Once you have a copy of the source, you can embed it in your own Python package, or install it into your site-packages easily:

```sh
$ cd pytboss
$ python -m pip install .
```

## Supported Models

The following models should be supported. Note however that only the `PBV4PS2` model has been tested.

<!-- GRILLS START -->

*  [Lexington Wi-Fi Upgrade](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/ghost%20grill.png)
*  [PB0500SP](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/500sp-109.png)
*  [PB0820SP/SPW](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB820sp.png)
*  [PB1000D3](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/ghost%20grill.png)
*  [PB1000NC1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1000NC1.png)
*  [PB1000NXW](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1000NX.png)
*  [PB1000PL](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/LAREDO-1000-2020-3-18-103.png)
*  [PB1000R1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1000R1.png)
*  [PB1000R2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1000R2-2019-11-29-abby-112.png)
*  [PB1000S1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1000S1.png)
*  [PB1000SC1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1000SC-2019-7-4-Abby-34.png)
*  [PB1000SC2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/pb1000sc2.png)
*  [PB1000SP](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1000sp.png)
*  [PB1000T1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1000T1-2019-7-4-abby-35.png)
*  [PB1000T2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1000T2-2018-11-28-abby-36.png)
*  [PB1000T3](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1000T3-117.png)
*  [PB1000T4](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1000t4-115.png)
*  [PB1000XL/PB1000SC3](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1000SC3-Front-101619.png)
*  [PB1000XLW1 (Austin XL)](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/austin-xl.png)
*  [PB1020CS2](https://dansons-mobile.s3.dualstack.us-east-1.amazonaws.com/grill-images/PB1020CS2.1.png)
*  [PB1020NXW](https://dansons-mobile.s3.dualstack.us-east-1.amazonaws.com/grill-images/PB1020NX.png)
*  [PB1100HTC](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1100HTC.M-Line%20Heritage.png)
*  [PB1100PS1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/1100ps.png)
*  [PB1100PSC1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1100PSC.png)
*  [PB1100PSC2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1100PSC-126.png)
*  [PB1100PSC3](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1100PSC3.png)
*  [PB1100SP/SPW](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1100sp.png)
*  [PB1100SPW2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1100SPW2.png)
*  [PB1150 PS3](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1150PS3.png)
*  [PB1150DX](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1150DX.png)
*  [PB1150G/GW](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/Nav-1150.png)
*  [PB1150PS2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1150PS2-2020-5-22-107.png)
*  [PB1150PS3](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1150PS3.png)
*  [PB1230](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/pitboss-logo-transparent.png)
*  [PB1230CS1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1230CS1-2021-9-17-tank-with-cover116.png)
*  [PB1230G/GW](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1230G-2019-10-21-109.png)
*  [PB1230SP/SPW](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1230sp-112.png)
*  [PB1250 CS2](https://dansons-mobile.s3.dualstack.us-east-1.amazonaws.com/grill-images/PB1250CS2.2.png)
*  [PB1250 NX](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1250NX.png)
*  [PB1250 PL](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1250PL.%20no%20shadow.png)
*  [PB1250CS](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1250CS-2021-10-14-EN-124.png)
*  [PB1250CS2](https://dansons-mobile.s3.dualstack.us-east-1.amazonaws.com/grill-images/PB1250CS2.2.png)
*  [PB1250NX](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1250NX.png)
*  [PB1250PL](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1250PL.%20no%20shadow.png)
*  [PB1285KC](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1285KC-103.png)
*  [PB1285PC](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1285PC.Rona.png)
*  [PB1300 M](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1300M.png)
*  [PB1300 PS4](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1300PS4.png)
*  [PB1300M](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1300M.png)
*  [PB1300PS4](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1300PS4.png)
*  [PB1450CS](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1450CS-2021-10-14-124.png)
*  [PB1500NXW](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1500NX.png)
*  [PB1600 CS](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1600CS-2021-10-14-EN-119.png)
*  [PB1600 CS2](https://dansons-mobile.s3.dualstack.us-east-1.amazonaws.com/grill-images/PB1600CS2.2.png)
*  [PB1600 M](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1600M.png)
*  [PB1600 PSE](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1600PS.Elite2024.png)
*  [PB1600AMB](https://dansons-mobile.s3.dualstack.us-east-1.amazonaws.com/grill-images/PB1600AMB.png)
*  [PB1600CS](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1600CS-2021-10-14-EN-119.png)
*  [PB1600CS2](https://dansons-mobile.s3.dualstack.us-east-1.amazonaws.com/grill-images/PB1600CS2.2.png)
*  [PB1600CST](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1600CST.png)
*  [PB1600M](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1600M.png)
*  [PB1600PS1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1600PS1-2020-5-22-single-105.png)
*  [PB1600PS2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1600PS2-2021-8-17-120.png)
*  [PB1600PS3](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1600PS3.png)
*  [PB1600PS3_](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1600PS3.png)
*  [PB1600PSE](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1600PS.Elite2024.png)
*  [PB1600SPW](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1600SPW.png)
*  [PB1600SPW2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB1600SPW2.png)
*  [PB2180LK](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB2180LK-2020-3-16-105.png)
*  [PB340](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB340.png)
*  [PB340TGW1 (Tailgator)](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB340TGW1-2018-9-20-abby-22.png)
*  [PB440D](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB440D3-2019-4-18-abby.png)
*  [PB440D2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB440D2-2019-1-7-abby-23.png)
*  [PB440D3/PB456D](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/pb456d.png)
*  [PB440TG1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB440TG1.png)
*  [PB440TGNC1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB440TGNC1.png)
*  [PB440TGR1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB440TGR1.png)
*  [PB550G](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/550-nav-109.png)
*  [PB700D](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB700D.Canada.png)
*  [PB700FB](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/pitboss700FB.png)
*  [PB700FBM2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB700FBM2.png)
*  [PB700FBW2 (Classic)](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/Copper-classic-pb700-8.png)
*  [PB700NC1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/pb700nc1-110.png)
*  [PB700R1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB700R1-2019-1-22-abby-21.png)
*  [PB700R2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB700R2-2019-7-1-Abby-111.png)
*  [PB700S](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/pb700s.png)
*  [PB700S1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB700S1-26.png)
*  [PB700S2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB700S2-2019-1-22-abby-27.png)
*  [PB700SC](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB700SC-2019-1-22-abby-125.png)
*  [PB700T1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB700T1-114.png)
*  [PB820CS1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB820CS1-2021-7-20-28.png)
*  [PB820D](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB820D.png)
*  [PB820D2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB820D2-2019-1-7-abby-29.png)
*  [PB820D3](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB0820D3-2019-11-7-EN-AN-FR-116.png)
*  [PB820D4](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB820D4.png)
*  [PB820FB](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/pb820fb.png)
*  [PB820FBC](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB820FBC.png)
*  [PB820PS1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/pb820ps1.png)
*  [PB820S](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/pb_820S.png)
*  [PB820SC](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB820SC.png)
*  [PB820T1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB820T1.png)
*  [PB820XL/PB820ME](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/820xl.png)
*  [PB850AMB](https://dansons-mobile.s3.dualstack.us-east-1.amazonaws.com/grill-images/PB850AMB.png)
*  [PB850CS1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB820CS1-2021-7-20-28.png)
*  [PB850CS2](https://dansons-mobile.s3.dualstack.us-east-1.amazonaws.com/grill-images/PB850CS2.2.png)
*  [PB850DX](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB850DX.png)
*  [PB850G/GW](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/850-nav-109.png)
*  [PB850M](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB850M.png)
*  [PB850PS2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB850PS2-2020-5-26-107.png)
*  [PB850SPW2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PB850SPW2.png)
*  [PBV3 M](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PBV3M.png)
*  [PBV3M](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PBV3M.png)
*  [PBV3NX](https://dansons-mobile.s3.dualstack.us-east-1.amazonaws.com/grill-images/PBV3NX.1.png)
*  [PBV4DX](https://dansons-mobile.s3.dualstack.us-east-1.amazonaws.com/grill-images/PBV4DX.png)
*  [PBV4NXW](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PBV4NX.png)
*  [PBV4PS2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PBV4PS2.png)
*  [PBV5 P2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/V5%20Competition.png)
*  [PBV5CS](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PBV5CS1-2021-6-17-121.png)
*  [PBV5P2](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/V5%20Competition.png)
*  [PBV5PL](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PBV5PL-2020-5-6-Brunswick-104.png)
*  [PBV6 M](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PBV6M.png)
*  [PBV6 PSE](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/V6%20Elite.png)
*  [PBV6M](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PBV6M.png)
*  [PBV6PSE](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/V6%20Elite.png)
*  [PBV7PW1](https://dansons-mobile.s3.us-east-1.amazonaws.com/grill-images/PBV7PW1_Sportsman-2021-6-30-controller123.png)

<!-- GRILLS END -->
