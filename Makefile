.PHONY: build vet run demo conflict
build:
	go build ./...
vet:
	go vet ./...
run:
	go run ./cmd/atlas
demo:
	python3 scripts/walkthrough.py --workflow submit-render --out var/demo
conflict:
	python3 scripts/conflict_demo.py --scenario all --out var/conflict
