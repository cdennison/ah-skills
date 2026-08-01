from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).parents[1] / "streamlit_app.py"


def test_search_flow_when_query_is_submitted() -> None:
    # Given
    app = AppTest.from_file(str(APP_PATH)).run(timeout=15)

    # When
    app.text_input(key="query").input("excel spreadsheets")
    app.button(key="search").click()
    app.run(timeout=30)

    # Then
    assert not app.exception
    assert app.title[0].value == "Agent skill search"
    assert app.dataframe[0].value.shape[0] == 12
    assert "Security_Scan" in app.dataframe[0].value.columns
