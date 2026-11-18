from ..commands import Login, RegisterAccount
from ..errors import ApplicationError, ErrorCode
from ..models.accounts import AccountView, LoginResult
from ..models.common import RequestContext
from ..ports import AccountRepository, PasswordHasher


class AccountService:
    def __init__(self, repository: AccountRepository, passwords: PasswordHasher) -> None:
        self.repository = repository
        self.passwords = passwords

    def register(self, context: RequestContext, command: RegisterAccount) -> AccountView:
        username = command.username.strip()

        if not username or not command.password.value:
            raise ApplicationError(ErrorCode.INVALID, "Username and password are required")

        password_hash = self.passwords.hash(command.password)
        return self.repository.create_account(username, password_hash)

    def login(self, context: RequestContext, command: Login) -> LoginResult:
        record = self.repository.credential_record(command.username.strip())

        if record is None or not self.passwords.verify(command.password, record[1]):
            raise ApplicationError(ErrorCode.UNAUTHENTICATED, "Invalid username or password")

        account = record[0]
        session_reference = self.repository.establish_login(account.id, context.client_session_id)

        return LoginResult(account, session_reference)

    def logout(self, context: RequestContext) -> None:
        self.repository.end_login(context.client_session_id)

    def require_user(self, context: RequestContext) -> AccountView:
        account = self.repository.resolve_login(context.client_session_id)

        if account is None:
            raise ApplicationError(ErrorCode.UNAUTHENTICATED, "Please log in")

        if context.user_id is not None and context.user_id != account.id:
            raise ApplicationError(ErrorCode.FORBIDDEN, "Account does not match login")

        return account
