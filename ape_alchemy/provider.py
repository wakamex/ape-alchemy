import os
import random
import time
from collections.abc import Iterable
from typing import Any, Optional, cast

from ape.api import ReceiptAPI, TraceAPI, TransactionAPI, UpstreamProvider, PluginConfig
from ape.exceptions import ContractLogicError, ProviderError, VirtualMachineError
from ape.logging import logger
from ape_ethereum.provider import Web3Provider
from ape_ethereum.trace import TransactionTrace
from eth_pydantic_types import HexBytes
from eth_typing import HexStr
from web3 import HTTPProvider, Web3
from web3.exceptions import ContractLogicError as Web3ContractLogicError
from web3.gas_strategies.rpc import rpc_gas_price_strategy
from web3.middleware import geth_poa_middleware

from .exceptions import AlchemyFeatureNotAvailable, AlchemyProviderError, MissingProjectKeyError

# The user must either set one of these or an ENV VAR of the pattern:
#  WEB3_<ECOSYSTEM>_<NETWORK>_PROJECT_ID or  WEB3_<ECOSYSTEM>_<NETWORK>_API_KEY
DEFAULT_ENVIRONMENT_VARIABLE_NAMES = ("WEB3_ALCHEMY_PROJECT_ID", "WEB3_ALCHEMY_API_KEY")

# Alchemy will try to publish private transactions for 25 blocks.
PRIVATE_TX_BLOCK_WAIT = 25


class AlchemyConfig(PluginConfig):
    """Configuration for Alchemy.

    Attributes:
        concurrency (int): The maximum number of concurrent requests to make.
            Defaults to 1.
        block_page_size (int): The maximum number of blocks to fetch in a single request.
            Defaults to 250,000.
        min_retry_delay (int): The amount of milliseconds to wait before retrying the request.
            Defaults to 1000 (one second).
        retry_backoff_factor (int): The multiplier applied to the retry delay after each failed
            attempt. Defaults to 2.
        max_retry_delay (int): The maximum length of the retry delay.
            Defaults to 30,000 (30 seconds).
        max_retries (int): The maximum number of retries.
            Defaults to 3.
        retry_jitter (int): A random number of milliseconds up to this limit is added to each retry
            delay. Defaults to 250 milliseconds.
    """

    concurrency: int = 1  # can't do exponential backoff with multiple threads
    block_page_size: int = 25_000_000  # this acts as an upper limit, safe to set very high
    min_retry_delay: int = 1_000  # 1 second
    retry_backoff_factor: int = 2  # exponential backoff
    max_retry_delay: int = 30_000  # 30 seconds
    max_retries: int = 3
    retry_jitter: int = 250  # 250 milliseconds


class Alchemy(Web3Provider, UpstreamProvider):
    """
    A web3 provider using an HTTP connection to Alchemy.

    Docs: https://docs.alchemy.com/alchemy/

    Args:
        network_uris: Dict[tuple, str]
            A mapping of (ecosystem_name, network_name) -> URI
    """

    network_uris: dict[tuple, str] = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        alchemy_config = cast(AlchemyConfig, self.config_manager.get_config("alchemy"))
        self.concurrency = alchemy_config.concurrency
        self.block_page_size = alchemy_config.block_page_size
        # overwrite for testing
        self.block_page_size = 1_000_000
        self.block_page_size = 2000
        self.concurrency = 1

    @property
    def uri(self):
        """
        Alchemy RPC URI, including the project ID.
        """
        ecosystem_name = self.network.ecosystem.name
        network_name = self.network.name
        if (ecosystem_name, network_name) in self.network_uris:
            return self.network_uris[(ecosystem_name, network_name)]

        key = None

        expected_env_var_prefix = f"WEB3_{ecosystem_name.upper()}_{network_name.upper()}_ALCHEMY"
        options = (
            *DEFAULT_ENVIRONMENT_VARIABLE_NAMES,
            f"{expected_env_var_prefix}_PROJECT_ID",
            f"{expected_env_var_prefix}_API_KEY",
        )

        for env_var_name in options:
            env_var = os.environ.get(env_var_name)
            if env_var:
                key = env_var
                break

        if not key:
            raise MissingProjectKeyError(options)

        network_formats_by_ecosystem = {
            "ethereum": "https://eth-{0}.g.alchemy.com/v2/{1}",
            "arbitrum": "https://arb-{0}.g.alchemy.com/v2/{1}",
            "base": "https://base-{0}.g.alchemy.com/v2/{1}",
            "optimism": "https://opt-{0}.g.alchemy.com/v2/{1}",
            "polygon": "https://polygon-{0}.g.alchemy.com/v2/{1}",
        }

        network_format = network_formats_by_ecosystem[ecosystem_name]
        uri = network_format.format(self.network.name, key)
        self.network_uris[(ecosystem_name, network_name)] = uri
        return uri

    @property
    def http_uri(self) -> str:
        # NOTE: Overriding `Web3Provider.http_uri` implementation
        return self.uri

    @property
    def ws_uri(self) -> str:
        # NOTE: Overriding `Web3Provider.ws_uri` implementation
        return "ws" + self.uri[4:]  # Remove `http` in default URI w/ `ws`

    @property
    def connection_str(self) -> str:
        return self.uri

    def connect(self):
        self._web3 = Web3(HTTPProvider(self.uri))
        try:
            # Any chain that *began* as PoA needs the middleware for pre-merge blocks
            ethereum_goerli = 5
            base = (8453, 84531)
            optimism = (10, 420)
            polygon = (137, 80001)

            if self._web3.eth.chain_id in (ethereum_goerli, *base, *optimism, *polygon):
                self._web3.middleware_onion.inject(geth_poa_middleware, layer=0)

            self._web3.eth.set_gas_price_strategy(rpc_gas_price_strategy)
        except Exception as err:
            raise ProviderError(f"Failed to connect to Alchemy.\n{repr(err)}") from err

    def disconnect(self):
        self._web3 = None

    def _get_prestate_trace(self, transaction_hash: str) -> dict:
        return self.make_request(
            "debug_traceTransaction", [transaction_hash, {"tracer": "prestateTracer"}]
        )

    def get_transaction_trace(self, transaction_hash: str, **kwargs) -> TraceAPI:
        if "debug_trace_transaction_parameters" not in kwargs:
            kwargs["debug_trace_transaction_parameters"] = {}
            return TransactionTrace(transaction_hash=transaction_hash, **kwargs)

    def get_virtual_machine_error(self, exception: Exception, **kwargs) -> VirtualMachineError:
        txn = kwargs.get("txn")
        if not hasattr(exception, "args") or not len(exception.args):
            return VirtualMachineError(base_err=exception, txn=txn)

        args = exception.args
        message = args[0]
        if (
            not isinstance(exception, Web3ContractLogicError)
            and isinstance(message, dict)
            and "message" in message
        ):
            # Is some other VM error, like gas related
            return VirtualMachineError(message["message"], txn=txn)

        elif not isinstance(message, str):
            return VirtualMachineError(base_err=exception, txn=txn)

        # If get here, we have detected a contract logic related revert.
        message_prefix = "execution reverted"
        if message.startswith(message_prefix):
            message = message.replace(message_prefix, "")

            if ":" in message:
                # Was given a revert message
                message = message.split(":")[-1].strip()
                return ContractLogicError(revert_message=message, txn=txn)
            else:
                # No revert message
                return ContractLogicError(txn=txn)

        return VirtualMachineError(message=message, txn=txn)

    def make_request(
        self,
        endpoint: str,
        parameters: Optional[Iterable] = None,
        min_retry_delay: Optional[int] = None,
        retry_backoff_factor: Optional[int] = None,
        max_retry_delay: Optional[int] = None,
        max_retries: Optional[int] = None,
        retry_jitter: Optional[int] = None,
    ) -> Any:
        print(f"{parameters=}")
        alchemy_config = cast(AlchemyConfig, self.config_manager.get_config("alchemy"))
        min_retry_delay = (
            min_retry_delay if min_retry_delay is not None else alchemy_config.min_retry_delay
        )
        retry_backoff_factor = (
            retry_backoff_factor
            if retry_backoff_factor is not None
            else alchemy_config.retry_backoff_factor
        )
        max_retry_delay = (
            max_retry_delay if max_retry_delay is not None else alchemy_config.max_retry_delay
        )
        max_retries = max_retries if max_retries is not None else alchemy_config.max_retries
        retry_jitter = retry_jitter if retry_jitter is not None else alchemy_config.retry_jitter
        for attempt in range(max_retries):
            try:
                return super().make_request(endpoint, parameters)
            except ProviderError as err:
                print(f"{err=}")
                # safely get error message
                message = str(err)

                # handle known error messages and continue
                if "this block range should work:" in message:
                    # extract block from error message: this block range should work: [0xef9020, 0xf0e791]
                    # extract pieces within the square brackets
                    block_range_match = re.search(r"\[(0x[0-9a-fA-F]+),\s*(0x[0-9a-fA-F]+)\]", message)
                    if block_range_match:
                        block_start_hex = block_range_match.group(1)
                        block_start = int(block_start_hex, 16)  # Convert from hex string
                        block_end_hex = block_range_match.group(2)
                        block_end = int(block_end_hex, 16)
                        block_range_int = block_start, block_end
                        block_range_hex = f"{block_start:x}", f"{block_end:x}"
                        print(f"Block range int: {block_range_int}")
                        print(f"Block range hex: {block_range_hex}")
                    else:
                        raise AlchemyProviderError("No valid block range found in the message.")
                    final_block = parameters["toBlock"]
                    new_params = parameters.copy()
                    new_params["toBlock"] = block_end_hex
                    results = []
                    results = super().make_request(endpoint, new_params)
                    for d in dir(results):
                        if not d.startswith("_"):
                            print(f"{d=}")
                    raise AlchemyProviderError("Testing.")
                elif any(
                    error in message
                    for error in ["exceeded its compute units", "Too Many Requests for url"]
                ):
                    retry_interval = min(
                        max_retry_delay, min_retry_delay * retry_backoff_factor**attempt
                    )
                    logger.info(
                        "Alchemy compute units exceeded, retrying, attempt %s/%s in %s ms",
                        attempt + 1,
                        max_retries,
                        retry_interval,
                    )
                    delay = retry_interval + random.randint(0, retry_jitter)
                    time.sleep(delay / 1000)
                    continue

                # freak out if we get here
                cls = (
                    AlchemyFeatureNotAvailable
                    if "is not available" in message
                    else AlchemyProviderError
                )
                raise cls(message) from err
        raise AlchemyProviderError(f"Rate limit exceeded after {max_retries} attempts.")

    def send_private_transaction(self, txn: TransactionAPI, **kwargs) -> ReceiptAPI:
        """
        See `Alchemy's guide <https://www.alchemy.com/overviews/ethereum-private-transactions>`__
        for more information on sending private transaction using Alchemy.
        For more information on the API itself, see its
        `REST reference <https://docs.alchemy.com/reference/eth-sendprivatetransaction>`__.

        Args:
            txn: (:class:`~ape.api.transactionsTransactionAPI`): The transaction.
            **kwargs: Kwargs here are used for private-transaction "preferences".

        Returns:
            :class:`~ape.api.transactions.ReceiptAPI`
        """
        max_block_number = kwargs.pop("max_block_number", None)

        params = {
            "tx": HexBytes(txn.serialize_transaction()).hex(),
            "maxBlockNumber": max_block_number,
        }
        if kwargs and "fast" not in kwargs:
            # If sending preferences, `fast` must be present.
            kwargs["fast"] = False
            params["preferences"] = kwargs

        try:
            txn_hash = self._make_request("eth_sendPrivateTransaction", [params])
        except (ValueError, Web3ContractLogicError) as err:
            vm_err = self.get_virtual_machine_error(err, txn=txn)
            raise vm_err from err

        # Since Alchemy will attempt to publish for 25 blocks,
        # we add 25 * block_time to the timeout.
        timeout = (
            PRIVATE_TX_BLOCK_WAIT * self.network.block_time
            + self.network.transaction_acceptance_timeout
        )

        receipt = self.get_receipt(
            txn_hash,
            required_confirmations=(
                txn.required_confirmations
                if txn.required_confirmations is not None
                else self.network.required_confirmations
            ),
            timeout=timeout,
        )
        logger.info(
            f"Confirmed {receipt.txn_hash} (private) (total fees paid = {receipt.total_fees_paid})"
        )
        self.chain_manager.history.append(receipt)
        return receipt

    def get_receipt(
        self,
        txn_hash: str,
        required_confirmations: int = 0,
        timeout: Optional[int] = None,
        **kwargs,
    ) -> ReceiptAPI:
        if not required_confirmations and not timeout:
            # Allows `get_receipt` to work better when not sending.
            data = self.web3.eth.get_transaction_receipt(HexStr(txn_hash))
            txn = dict(self.web3.eth.get_transaction(HexStr(txn_hash)))
            return self.network.ecosystem.decode_receipt(
                {
                    "provider": self,
                    "required_confirmations": required_confirmations,
                    **txn,
                    **data,
                }
            )
        # Sending txns will get here because they always pass in required confs.
        return super().get_receipt(
            txn_hash, required_confirmations=required_confirmations, timeout=timeout, **kwargs
        )
