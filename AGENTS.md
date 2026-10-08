# Development instructions

Never place real-money orders while reviewing, debugging, testing or preparing
this project for publication. Use mocked IBKR clients and offline historical
replay. Do not start a live broker connection to verify a code change.

Keep credentials, webhook tokens, account identifiers and private broker logs
out of code, commits, screenshots, test fixtures and assistant output. Report
security findings using file/line references and redacted descriptions.

Run the offline regression tests and publication guard after security changes.
Never publish or deploy this repository as part of an audit.
