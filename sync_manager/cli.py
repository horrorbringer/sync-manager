import click
from cryptography.fernet import Fernet

from . import db
from .models import User


def register_commands(app):
    @app.cli.command("init-db")
    def init_db():
        db.create_all()
        click.echo("Management database initialized.")

    @app.cli.command("create-user")
    @click.option("--username", prompt=True)
    @click.option("--role", type=click.Choice(["administrator", "operator", "viewer"]), prompt=True)
    @click.password_option()
    def create_user(username, role, password):
        if db.session.scalar(db.select(User).filter_by(username=username)):
            raise click.ClickException("Username already exists")
        user = User(username=username, role=role)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo("User created.")

    @app.cli.command("generate-encryption-key")
    def generate_encryption_key():
        click.echo(Fernet.generate_key().decode())

