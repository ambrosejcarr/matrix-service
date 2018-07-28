default: all

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
		--secret-id matrix-service/dev/terraform.tfvars | \
		jq -r .SecretString | \
		python -m json.tool | \
		tee terraform/terraform.tfvars chalice/chalicelib/config.json

.PHONY: build
build:
	bash -c 'for wheel in vendor.in/*/*.whl; do unzip -q -o -d chalice/vendor/ $$wheel; done'
	. venv/bin/activate && cd chalice && chalice package ../target/ && rm -rf vendor/

.PHONY: deploy
deploy:
	aws s3api create-bucket --bucket $(shell aws secretsmanager get-secret-value --secret-id \
	matrix-service/dev/terraform.tfvars | jq -r .SecretString | jq -r .hca_ms_deployment_bucket) \
	--region us-east-1 --acl private
	@read -p "Enter the version number of the service to deploy: " app_version; \
	aws s3 cp target/deployment.zip s3://$(shell aws secretsmanager get-secret-value --secret-id \
	matrix-service/dev/terraform.tfvars | jq -r .SecretString | jq -r .hca_ms_deployment_bucket)\
	/v$$app_version/deployment.zip; 
	cd terraform && terraform init && terraform apply
	rm -rf target

# Undeploy all aws fixtures
.PHONY: clean
clean:
	aws s3 rb s3://$(shell aws secretsmanager get-secret-value --secret-id \
	matrix-service/dev/terraform.tfvars | jq -r .SecretString | jq -r .hca_ms_deployment_bucket) \
	--force
	cd terraform && terraform destroy
	rm -rf target
	rm terraform/terraform.tfvars
	rm -rf venv
	rm chalice/chalicelib/config.json

.PHONY: all
all:
	make install && make secrets && make build && make deploy && make test
