package main

import (
	"log"

	"github.com/kushalsrinivas/web-research-ai-engine/internal/platform/service"
)

func main() {
	if err := service.Run("fetcher", 8082, nil); err != nil {
		log.Fatal(err)
	}
}
