PROJECT_CONFIG_SECRET = matrix-service/dev/terraform.tfvars
TERRAFORM_BACKEND_SECRET = matrix-service/dev/backend.tfvars

default: all

.PHONY: init
init:
	aws secretsmanager get-secret-value \
		--secret-id $(TERRAFORM_BACKEND_SECRET) | \
		jq -r .SecretString | \
		python -m json.tool | \
		tee terraform/backend.auto.tfvars
	cd terraform && terraform init -backend-config=backend.auto.tfvars

.PHONY: install
install:
	virtualenv -p python3 venv
	. venv/bin/activate && pip install -r requirements-dev.txt --upgrade

.PHONY: test
test:
	. venv/bin/activate && PYTHONPATH=chalice \
		python -m unittest discover -s tests -p '*_tests.py'

.PHONY: secrets
secrets:
	aws secretsmanager get-secret-value \
		--secret-id $(PROJECT_CONFIG_SECRET) | \
		jq -r .SecretString | \
		python -m json.tool | \
		tee terraform/terraform.tfvars

.PHONY: data
data:
	rm -rf data
	mkdir data
	aws s3 sync s3://matrix-service-performance-data data/

.PHONY: clean-data
clean-data:
	rm -rf data

.PHONY: upload-secrets
upload-secrets:
	python scripts/upload_project_secrets.py $(PROJECT_CONFIG_SECRET)

.PHONY: build
build:
	bash -c 'for wheel in vendor.in/*/*.whl; do unzip -q -o -d chalice/vendor/ $$wheel; done'
	. venv/bin/activate && cd chalice && chalice package ../target/ && rm -rf vendor/

.PHONY: deploy
deploy:
	aws s3api create-bucket --bucket $(shell aws secretsmanager get-secret-value --secret-id \
	$(PROJECT_CONFIG_SECRET) | jq -r .SecretString | jq -r .hca_ms_deployment_bucket)
	@read -p "Enter the version number of the service to deploy: " app_version; \
	aws s3 cp target/deployment.zip s3://$(shell aws secretsmanager get-secret-value --secret-id \
	matrix-service/dev/terraform.tfvars | jq -r .SecretString | jq -r .hca_ms_deployment_bucket)\
	/v$$app_version/deployment.zip;
	cd terraform && terraform apply
	rm -rf target

.PHONY: clean
clean:
	cd terraform && terraform destroy
	aws s3 rb s3://$(shell aws secretsmanager get-secret-value --secret-id \
	$(PROJECT_CONFIG_SECRET) | jq -r .SecretString | jq -r .hca_ms_deployment_bucket) --force
	rm -rf target
	rm terraform/*.tfvars
	rm -rf venv
	rm -rf data

.PHONY: all
all:
	make init && make install && make secrets && make build && make deploy && make test
