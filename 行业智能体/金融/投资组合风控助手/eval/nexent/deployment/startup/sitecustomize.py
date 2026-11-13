"""Optional startup hook for the dedicated runtime container only."""
import os


if os.environ.get("TOPIC16_RECEIPTS_DIR") or os.environ.get("TOPIC16_OBSERVATION_RUN_ID"):
    from topic16_receipt_sink import PrivateJSONLReceiptSink, install_receipt_sink
    allowed_tools = ('evaluate_portfolio_snapshot',)
    if os.environ.get('TOPIC16_OBJECT_RECEIPTS') == '1':
        from topic16_object_observation import install, OBJECT_TOOLS
        install()
        allowed_tools += tuple(OBJECT_TOOLS)

    sink = PrivateJSONLReceiptSink(
        os.environ["TOPIC16_RECEIPTS_DIR"],
        os.environ["TOPIC16_OBSERVATION_RUN_ID"],
        allowed_tools=allowed_tools,
    )
    if os.environ.get('TOPIC16_WORKFLOW_AGENT'):
        from topic16_workflow_dispatch import install as install_workflow, stamp_receipts
        install_workflow(os.environ['TOPIC16_WORKFLOW_AGENT'],
                         os.environ['TOPIC16_DISPATCH_KEY_FILE'],
                         os.environ['TOPIC16_RECEIPTS_DIR'])
        sink = stamp_receipts(sink)
    install_receipt_sink(sink)
