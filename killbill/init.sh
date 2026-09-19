#!/bin/bash
set -e
echo "Installing curl and downloading Kill Bill Schemas..."
apt-get update && apt-get install -y curl

# Create the Kaui database
mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" -e "CREATE DATABASE IF NOT EXISTS kaui;"

# Download and import the tables straight from Kill Bill's official Github
curl -sL "https://raw.githubusercontent.com/killbill/killbill/master/util/src/main/resources/org/killbill/billing/server/ddl.sql" | mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" killbill

curl -sL "https://raw.githubusercontent.com/killbill/killbill-admin-ui/master/db/ddl.sql" | mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" kaui

echo "Schema import complete!"
