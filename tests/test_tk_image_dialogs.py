from thermal_monitoring.tools.tk_image_dialogs import fit_image_rect


def test_fit_image_rect_preserves_aspect_ratio_and_centers_image():
    rect = fit_image_rect(640, 480, 0, 0, 1000, 500)

    assert (rect.width, rect.height) == (666, 500)
    assert rect.x == 167
    assert rect.y == 0


def test_image_rect_coordinate_round_trip_after_resize():
    for area_width, area_height in ((800, 400), (1100, 650), (600, 700)):
        rect = fit_image_rect(640, 480, 0, 0, area_width, area_height)
        canvas_point = rect.to_canvas(321, 239)
        restored = rect.to_source(*canvas_point)

        assert abs(restored[0] - 321) <= 1
        assert abs(restored[1] - 239) <= 1


def test_image_rect_rejects_letterbox_area():
    rect = fit_image_rect(640, 480, 0, 0, 1000, 500)

    assert not rect.contains(20, 250)
    assert rect.contains(rect.x, rect.y)
