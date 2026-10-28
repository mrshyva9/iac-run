.PHONY: test doctor tree ls

test:
	python3 -m unittest discover -s tests -v

doctor:
	./bin/modops doctor

tree:
	./bin/modops tree

ls:
	./bin/modops ls
