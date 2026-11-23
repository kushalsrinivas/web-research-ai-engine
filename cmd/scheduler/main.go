package main

import (
	"log"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/service"
)

func main() {
	if err := service.Run("scheduler", 8081, nil); err != nil {
		log.Fatal(err)
	}
}
