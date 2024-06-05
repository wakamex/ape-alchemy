import pytest
from requests import HTTPError
from unittest.mock import MagicMock
from ape_alchemy.provider import AlchemyProviderError

@pytest.fixture
def alchemy_instance(mock_web3, alchemy_provider):
    alchemy_provider._web3 = mock_web3

    # Mock the _make_request method to always raise the AlchemyProviderError
    # mock_make_request = MagicMock(side_effect=AlchemyProviderError("Rate limit exceeded"))
    # alchemy_provider._make_request = mock_make_request
    return alchemy_provider

def test_exponential_backoff(alchemy_instance, mocker):
    error_response = MagicMock()
    error_response.json.return_value = {"error": {"message": "exceeded its compute units"}}
    error = HTTPError(response=error_response)

    # Mock the _make_request method to always raise the HTTPError
    mock_make_request = mocker.patch.object(alchemy_instance, '_make_request', side_effect=error)

    with pytest.raises(AlchemyProviderError, match="Rate limit exceeded after 3 attempts."):
        alchemy_instance._make_request("some_endpoint", [])

    assert mock_make_request.call_count == alchemy_instance.config.max_retries

# Additional tests to cover other aspects of exponential backoff can be added here.
