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
            # fmt: off
            "p_1_Set_Temp":  165,
            "p_1_Temp":      191,
            "p_2_Temp":      192,
            "p_3_Temp":      960,
            "p_4_Temp":      960,
            "smokerActTemp": 220,
            "grillSetTemp":  225,
            "moduleIsOn":    True,
            "err_1":         False,
            "err_2":         False,
            "err_3":         False,
            "tempHighErr":   False,
            "fanErr":        False,
            "hotErr":        False,
            "motorErr":      False,
            "noPellets":     False,
            "erL":           False,
            "fanState":      True,
            "hotState":      True,
            "motorState":    True,
            "lightState":    False,
            "primeState":    True,
            "isFahrenheit":   True,
            "recipeStep":    4,
            "time_H":        12,
            "time_M":        59,
            "time_S":        31,
            # fmt: on
        }

    def test_decode_status_grill_temp(self):
        data = (
            "FE 0B 01 06 05 01 09 01 01 09 02 09 06 00 09 06 00 02 02 00 02 02 "
            "05 02 01 00 00 00 00 00 00 00 00 00 01 01 01 00 01 01 04 0C 3B 1F"
        )
        status = api.decode_state("".join(data.split()))
        assert status == {
            # fmt: off
            "p_1_Set_Temp":  165,
            "p_1_Temp":      191,
            "p_2_Temp":      192,
            "p_3_Temp":      960,
            "p_4_Temp":      960,
            "smokerActTemp": 220,
            "grillTemp":     225,
            "moduleIsOn":    True,
            "err_1":         False,
            "err_2":         False,
            "err_3":         False,
            "tempHighErr":   False,
            "fanErr":        False,
            "hotErr":        False,
            "motorErr":      False,
            "noPellets":     False,
            "erL":           False,
            "fanState":      True,
            "hotState":      True,
            "motorState":    True,
            "lightState":    False,
            "primeState":    True,
            "isFahrenheit":   True,
            "recipeStep":    4,
            "time_H":        12,
            "time_M":        59,
            "time_S":        31,
            # fmt: on
        }

    def test_decode_all_temps(self):
        data = (
            "FE 0C 01 07 00 01 05 00 01 06 05 09 06 00 "
            "09 06 00 02 02 00 02 02 05 02 02 00 01"
        )
        status = api.decode_state("".join(data.split()))
        assert status == {
            # fmt: off
            "p_1_Set_Temp":  170,
            "p_1_Temp":      150,
            "p_2_Temp":      165,
            "p_3_Temp":      960,
            "p_4_Temp":      960,
            "smokerActTemp": 220,
            "grillSetTemp":  225,
            "grillTemp":     220,
            "isFahrenheit":   True,
            # fmt: on
        }

    def test_decode_set_temps(self):
        data = "".join("FE 0D 02 02 05 01 07 00".split())
        print(data)
        status = api.decode_state(data)
        assert status == {
            "grillSetTemp": 225,
            "p_1_Set_Temp": 170,
        }
