package main

import (
	"log"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/service"
)

func main() {
	if err := service.Run("reconciler", 8084, nil); err != nil {
		log.Fatal(err)
	}
}
