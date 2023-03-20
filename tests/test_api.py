from datetime import timedelta

from pytboss import api


class TestHexToArray:
    def test_hex_to_array(self):
        assert [1, 5, 10, 16] == api.hex_to_array("01050a10")


class TestEncodeDecode:
    def test_encode_temp(self):
        assert "090909" == api.encode_temp(999)
        assert "020500" == api.encode_temp(250)
        assert "000500" == api.encode_temp(50)
        assert "000001" == api.encode_temp(1)

    def test_decode_temp(self):
        assert 999 == api.decode_temp(9, 9, 9)
        assert 250 == api.decode_temp(2, 5, 0)
        assert 50 == api.decode_temp(0, 5, 0)
        assert 1 == api.decode_temp(0, 0, 1)


class TestDecode:
    def test_decode_status_grill_set_temp(self):
        data = (
            "FE 0B 01 06 05 01 09 01 01 09 02 09 06 00 09 06 00 02 02 00 02 02 "
            "05 01 01 00 00 00 00 00 00 00 00 00 01 01 01 00 01 01 04 0C 3B 1F"
        )
        status = api.decode_state("".join(data.split()))
        assert status == {
            "temperatures": {
                "probe_1_target": 165,
                "probe_1_actual": 191,
                "probe_2_actual": 192,
                "probe_3_actual": 960,
                "probe_4_actual": 960,
                "smoker_actual": 220,
                "grill_target": 225,
                "is_fahrenheit": True,
            },
            "is_on": True,
            "error_1": False,
            "error_2": False,
            "error_3": False,
            "temp_high_error": False,
            "fan_error": False,
            "igniter_error": False,
            "auger_error": False,
            "no_pellets": False,
            "error_l": False,
            "fan_is_on": True,
            "igniter_is_on": True,
            "auger_is_on": True,
            "light_is_on": False,
            "prime_is_on": True,
            "recipe_step": {
                "step_number": 4,
                "time_remaining": timedelta(hours=12, minutes=59, seconds=31),
            },
        }

    def test_decode_status_grill_temp(self):
        data = (
            "FE 0B 01 06 05 01 09 01 01 09 02 09 06 00 09 06 00 02 02 00 02 02 "
            "05 02 01 00 00 00 00 00 00 00 00 00 01 01 01 00 01 01 04 0C 3B 1F"
        )
        status = api.decode_state("".join(data.split()))
        assert status == {
            "temperatures": {
                "probe_1_target": 165,
                "probe_1_actual": 191,
                "probe_2_actual": 192,
                "probe_3_actual": 960,
                "probe_4_actual": 960,
                "smoker_actual": 220,
                "grill_actual": 225,
                "is_fahrenheit": True,
            },
            "is_on": True,
            "error_1": False,
            "error_2": False,
            "error_3": False,
            "temp_high_error": False,
            "fan_error": False,
            "igniter_error": False,
            "auger_error": False,
            "no_pellets": False,
            "error_l": False,
            "fan_is_on": True,
            "igniter_is_on": True,
            "auger_is_on": True,
            "light_is_on": False,
            "prime_is_on": True,
            "recipe_step": {
                "step_number": 4,
                "time_remaining": timedelta(hours=12, minutes=59, seconds=31),
            },
        }

    def test_decode_all_temps(self):
        data = (
            "FE 0C 01 07 00 01 05 00 01 06 05 09 06 00 "
            "09 06 00 02 02 00 02 02 05 02 02 00 01"
        )
        status = api.decode_state("".join(data.split()))
        assert status == {
            "probe_1_target": 170,
            "probe_1_actual": 150,
            "probe_2_actual": 165,
            "probe_3_actual": 960,
            "probe_4_actual": 960,
            "smoker_actual": 220,
            "grill_target": 225,
            "grill_actual": 220,
            "is_fahrenheit": True,
        }

    def test_decode_target_temps(self):
        data = "".join("FE 0D 02 02 05 01 07 00".split())
        status = api.decode_state(data)
        assert status == {
            "grill_target": 225,
            "probe_1_target": 170,
        }
